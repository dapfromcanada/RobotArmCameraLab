"""camera_lab MCP server (stdio only) — the tool surface Claude uses to tune cameras.

SECURITY-SENSITIVE (strongest tier + human review of the tool surface):

- **stdio only.** ``camera-lab mcp`` calls ``server.run(transport="stdio")``; no
  SSE / HTTP transport is ever started. Logs go to stderr; stdout is the protocol.
- **Safety limits are read-only.** No tool can change them (``get_safety_limits``
  only reads). Every settings and light change goes through ``TwoDService`` /
  ``LightService`` and so through ``core/safety.py``; out-of-limit requests come
  back as structured rejections, never as exceptions.
- **Bounded, validated input.** Every argument has a schema with bounds
  (pydantic); ids and names are pattern-checked and resolved through
  ``safe_join`` downstream, so no argument can address a path.
- **One error shape.** Failures are returned as
  ``{"ok": false, "error": {"code": ..., "message": ...}}`` — including argument
  validation and unknown tools (rewritten by :class:`ErrorShapeMiddleware`).
  Stack traces are logged to stderr, never returned.
- Calls are serialised (one camera operation at a time). Cameras are opened on
  first use with lock owner ``mcp`` and stay locked until the server stops; on
  shutdown all cameras are closed and all lights turned off.
"""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp_types import CallToolResult, TextContent
from pydantic import Field, ValidationError

from camera_lab.core.lights import install_lights_off_handlers
from camera_lab.core.logging import get_logger
from camera_lab.two_d.errors import InvalidRequestError, TwoDError
from camera_lab.two_d.models import CameraSettingsPatch
from camera_lab.two_d.service import TwoDService, build_service

_log = get_logger(__name__)

ACTOR = "agent"
OWNER = "mcp"

# ---------------------------------------------------------------- arg types
CameraId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Camera serial (e.g. 233100505, U330439, SIM-0001) or alias (triton, apex, sim).",
    ),
]
RunId = Annotated[
    str, Field(pattern=r"^\d{8}-\d{6}_[A-Za-z0-9-]{1,32}_[a-z0-9-]{1,40}$", description="Run id.")
]
CaptureId = Annotated[
    str,
    Field(
        pattern=r"^\d{8}-\d{6}_[A-Za-z0-9-]{1,32}_[a-z0-9-]{1,40}/\d{4}$",
        description="Capture id '<run_id>/<NNNN>'.",
    ),
]
PresetName = Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,40}$", description="Preset name.")]
ModeName = Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,40}$", description="Profile mode name.")]
Note = Annotated[str, Field(max_length=500)]


@dataclass
class AppState:
    """What the tools share (built by the lifespan)."""

    service: TwoDService
    sim: bool
    lock: threading.Lock = field(default_factory=threading.Lock)


def _ok(**data: Any) -> dict[str, Any]:
    return {"ok": True, **data}


def _err(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


# ------------------------------------------------------- workflow text
def workflow_text(service: TwoDService) -> str:
    """The server ``instructions``: how to run a tuning loop and what the scores mean."""
    lim = service.limits.limits
    sp = service.scoring.default
    return f"""camera_lab tunes the robot cell's 2D cameras and LED lights. You (the agent) run a
capture → score → adjust loop inside hard safety limits that you cannot change.

Workflow
1. list_cameras, then get_camera_profile(camera_id): it says what the camera is (rolling or
   global shutter, Bayer or 3-sensor prism, HDR) and its modes. Each mode names its tuning
   strategy and the `knobs` (settings) that make sense to change in that mode.
2. set_camera_mode(camera_id, mode) if you want a mode other than the current one.
3. start_tuning_run(camera_id, goal, max_steps): returns run_id, the step budget, current
   settings, effective ranges and the scoring targets. Every capture, settings change and
   light change uses one step; when the budget is used up, further changes are refused.
4. Loop: capture(camera_id, run_id) → read the scores and look at the preview image →
   set_camera_settings / set_lights → capture again.
5. finish_tuning_run(run_id, best_capture_id, preset_name, summary) with the best capture;
   give a preset_name to save it. Call lights_off when you are done with the lights.
run_classic_tune runs the built-in non-AI tuner instead (useful as a baseline to compare).

Scores (0–1 levels of the pixel format's full scale; scoring.yaml)
- mean_level: average brightness; target {sp.target_mean} (± {sp.mean_pass_band} passes).
- clipped_pct: % of pixels with any channel at the maximum value; keep ≤ {sp.clip_max_pct} %.
- black_pct: % of pixels with all channels ≤ {sp.black_threshold} of full scale; keep ≤ {sp.black_max_pct} %.
- noise_sigma: estimated noise; keep ≤ {sp.noise_max}.
- sharpness: focus measure, compare only within one camera/scene; not in the composite.
- composite 0–100 = 100 × exposure × clipping × black × noise sub-scores. Higher is better.
A camera mode may override these targets (e.g. HDR modes use a lower mean target).

Limits (safety_limits.yaml, read-only — see get_safety_limits)
- Camera exposure, gain and frame rate: only the camera's own range limits them. Stay inside the
  profile's working_range (get_camera_settings → working_range): it is the sensible range for
  tuning, not a hard limit.
- Hard limits: lights ≤ {lim.lights.default.max_pct} %, changed in steps of ≤ {lim.lights.default.max_step_pct} %;
  ≤ {lim.mcp.max_capture_count} frames per capture call; step budget ≤ {lim.tuning.max_steps_hard};
  protected camera features (network, reset, firmware, user-set save) are never written.
- A request outside a limit or the camera range is REJECTED with a reason naming the limit.
  Rejections are normal feedback, not failures: read the reason and choose another value.
  Values off the camera's increment are rounded and reported as "clamped".
- Nobody can change limits through these tools. Do not try; work within them.
Notes: long exposures automatically lower the camera's frame rate (reported as a warning);
an HDR-mode Triton has read-only exposure/gain — tune its HDR channel knobs instead."""


# --------------------------------------------------------- error shape
class ErrorShapeMiddleware:
    """Rewrite SDK-generated tool errors (bad arguments, unknown tool) into our JSON shape."""

    async def __call__(self, ctx: Any, call_next: Callable[[Any], Any]) -> Any:
        result = await call_next(ctx)
        if getattr(ctx, "method", None) != "tools/call":
            return result
        res = result
        if isinstance(res, dict):
            try:
                res = CallToolResult.model_validate(res)
            except ValidationError:
                return result
        if not isinstance(res, CallToolResult) or not res.is_error:
            return result
        text = " ".join(c.text for c in res.content if isinstance(c, TextContent)).strip()
        if text.startswith("Unknown tool"):
            body = _err("unknown_tool", text[:300])
        elif "validation error" in text.lower() or "input should" in text.lower():
            body = _err("invalid_argument", text[:1000])
        else:
            body = _err("internal_error", "tool failed; see the server log")
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(body))], is_error=True
        )


# -------------------------------------------------------------- server
def create_server(
    sim: bool = False, service_factory: Callable[[], TwoDService] | None = None
) -> MCPServer:
    """Build the camera-lab MCP server (tools registered; service built in the lifespan)."""
    holder: dict[str, AppState] = {}

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[AppState]:
        svc = service_factory() if service_factory is not None else build_service(sim=sim)
        uninstall = (
            install_lights_off_handlers(svc.light_service.controller)
            if svc.light_service is not None and service_factory is None
            else None
        )
        state = AppState(svc, sim)
        holder["state"] = state
        _log.info("mcp_server_started", sim=sim)
        try:
            yield state
        finally:
            try:
                svc.shutdown()  # close cameras (release locks) + lights off
            finally:
                holder.pop("state", None)
                if uninstall is not None:
                    uninstall()
                _log.info("mcp_server_stopped")

    # Instructions are built from the live config, before the lifespan runs.
    preview_service = service_factory() if service_factory is not None else build_service(sim=sim)
    instructions = workflow_text(preview_service)
    if service_factory is None:
        preview_service.shutdown()

    server = MCPServer(
        name="camera-lab",
        instructions=instructions,
        lifespan=lifespan,
        middleware=[ErrorShapeMiddleware()],
    )

    def guarded(
        tool_name: str, camera_arg: str | None = None, log_call: bool = False
    ) -> Callable[..., Any]:
        """Run a tool body under the lock with the error shape; optionally log read-only calls."""

        def wrap(body: Callable[..., Any]) -> Callable[..., Any]:
            def run(*args: Any, **kwargs: Any) -> Any:
                state = holder.get("state")
                if state is None:
                    return _err("not_ready", "server is starting or stopping")
                with state.lock:
                    try:
                        result = body(state, *args, **kwargs)
                    except TwoDError as exc:
                        result = _err(exc.code, str(exc))
                    except ValidationError as exc:
                        result = _err(
                            "invalid_argument", "; ".join(e["msg"] for e in exc.errors())[:1000]
                        )
                    except Exception:
                        _log.exception("mcp_tool_failed", tool=tool_name)
                        result = _err("internal_error", f"{tool_name} failed; see the server log")
                    if log_call and camera_arg is not None:
                        _log_read(state, kwargs.get(camera_arg), tool_name, kwargs, result)
                    return result

            return run

        return wrap

    def _ensure_open(state: AppState, camera_id: str) -> str:
        svc = state.service
        serial = svc.resolve(camera_id)
        if not svc.is_open(serial):
            svc.open(serial, OWNER)
        return serial

    def _log_read(
        state: AppState, camera_id: Any, tool: str, args: dict[str, Any], result: Any
    ) -> None:
        if not isinstance(camera_id, str):
            return
        try:
            run = state.service.active_run(camera_id)
        except Exception:
            return
        if run is not None:
            body = result[0] if isinstance(result, list) else result
            ok = isinstance(body, dict) and body.get("ok", False)
            run.log(ACTOR, tool, _jsonable(args), {"ok": ok})

    # ---------------------------------------------------------- tools
    @server.tool(structured_output=False)
    def list_cameras(include_sim: bool = False) -> Any:
        """List connected 2D cameras (vendor, model, serial, firmware, interface, IP, driver)."""

        @guarded("list_cameras")
        def body(state: AppState) -> dict[str, Any]:
            cams = [
                c.model_dump(mode="json")
                for c in state.service.discover()
                if include_sim or c.driver != "simulated"
            ]
            return _ok(cameras=cams)

        return body()

    @server.tool(structured_output=False)
    def get_camera_profile(camera_id: CameraId) -> Any:
        """The camera's profile: characteristics and modes (strategy, knobs, scoring per mode)."""

        @guarded("get_camera_profile", "camera_id", log_call=True)
        def body(state: AppState, camera_id: str) -> dict[str, Any]:
            prof = state.service.get_profile(camera_id)
            mode = (
                state.service.current_mode(camera_id) if state.service.is_open(camera_id) else None
            )
            return _ok(profile=prof.model_dump(mode="json", exclude_none=True), current_mode=mode)

        return body(camera_id=camera_id)

    @server.tool(structured_output=False)
    def set_camera_mode(camera_id: CameraId, mode: ModeName) -> Any:
        """Enter a profile mode (applies the mode's settings through the safety limits)."""

        @guarded("set_camera_mode")
        def body(state: AppState) -> dict[str, Any]:
            serial = _ensure_open(state, camera_id)
            res = state.service.set_mode(serial, mode, ACTOR)
            return _ok(mode=mode, result=res.model_dump(mode="json"))

        return body()

    @server.tool(structured_output=False)
    def get_camera_settings(camera_id: CameraId) -> Any:
        """Current settings, effective allowed ranges (camera ∩ safety limits) and which settings affect calibration."""

        @guarded("get_camera_settings", "camera_id", log_call=True)
        def body(state: AppState, camera_id: str) -> dict[str, Any]:
            serial = _ensure_open(state, camera_id)
            view = state.service.get_settings(serial).to_dict()
            return _ok(**view, mode=state.service.current_mode(serial))

        return body(camera_id=camera_id)

    @server.tool(structured_output=False)
    def set_camera_settings(camera_id: CameraId, changes: CameraSettingsPatch) -> Any:
        """Apply setting changes. Each field is applied, clamped (rounded to the camera increment) or rejected with a reason; values are read back."""

        @guarded("set_camera_settings")
        def body(state: AppState) -> dict[str, Any]:
            serial = _ensure_open(state, camera_id)
            res = state.service.apply(serial, changes, ACTOR)
            return _ok(**res.model_dump(mode="json"), all_applied=res.ok)

        return body()

    @server.tool(structured_output=False)
    def capture(
        camera_id: CameraId,
        run_id: RunId | None = None,
        count: Annotated[int, Field(ge=1, le=5)] = 1,
        note: Note = "",
    ) -> Any:
        """Capture, score and save 1–5 frames (into run_id, else the active run, else a new run). Returns scores, the test charts seen (chart_id, version, tag ids) and a JPEG preview of the last frame."""

        @guarded("capture")
        def body(state: AppState) -> Any:
            svc = state.service
            max_count = svc.limits.limits.mcp.max_capture_count
            if count > max_count:
                raise InvalidRequestError(
                    f"count {count} > limit {max_count} ({svc.limits.label}: mcp.max_capture_count)"
                )
            serial = _ensure_open(state, camera_id)
            results = svc.capture(serial, count, run_id, note, ACTOR)
            data = _ok(captures=[r.to_dict() for r in results])
            last = results[-1].preview_path
            if last is not None and Path(last).is_file():
                return [data, Image(data=Path(last).read_bytes(), format="jpeg")]
            return data

        return body()

    @server.tool(structured_output=False)
    def score_capture(capture_id: CaptureId) -> Any:
        """Recompute the scores of a saved capture from disk."""

        @guarded("score_capture")
        def body(state: AppState) -> dict[str, Any]:
            report = state.service.score_capture(capture_id)
            return _ok(capture_id=capture_id, scores=report.model_dump(mode="json"))

        return body()

    @server.tool(structured_output=False)
    def list_lights() -> Any:
        """Light channels with current level (%), maximum allowed % and maximum step %."""

        @guarded("list_lights")
        def body(state: AppState) -> dict[str, Any]:
            return _ok(channels=state.service.list_lights())

        return body()

    @server.tool(structured_output=False)
    def set_lights(
        levels: Annotated[
            dict[str, Annotated[float, Field(ge=0, le=100)]], Field(min_length=1, max_length=16)
        ],
    ) -> Any:
        """Set light levels (0–100 % per channel). Above-limit levels are rejected; large changes are ramped."""

        @guarded("set_lights")
        def body(state: AppState) -> dict[str, Any]:
            res = state.service.set_lights(levels, ACTOR)
            return _ok(
                all_applied=res.ok,
                levels=res.levels,
                ramp_steps=res.ramp_steps,
                results=[r.__dict__ for r in res.results],
            )

        return body()

    @server.tool(structured_output=False)
    def lights_off() -> Any:
        """Turn all lights off (always allowed, uses no step)."""

        @guarded("lights_off")
        def body(state: AppState) -> dict[str, Any]:
            return _ok(levels=state.service.lights_off(ACTOR))

        return body()

    @server.tool(structured_output=False)
    def start_tuning_run(
        camera_id: CameraId,
        goal: Note,
        max_steps: Annotated[int, Field(ge=1, le=1000)],
    ) -> Any:
        """Start a step-budgeted tuning run; returns run_id, budget, settings, effective ranges, scoring targets and the camera profile/mode."""

        @guarded("start_tuning_run")
        def body(state: AppState) -> dict[str, Any]:
            svc = state.service
            serial = _ensure_open(state, camera_id)
            run = svc.start_run(serial, goal, max_steps, method="agent", operator="agent")
            view = svc.get_settings(serial).to_dict()
            prof = svc.get_profile(serial)
            mode = svc.current_mode(serial)
            return _ok(
                run_id=run.run_id,
                step_budget=run.budget.max_steps,
                steps_remaining=run.steps_remaining,
                settings=view["settings"],
                ranges=view["ranges"],
                working_range=view["working_range"],
                options=view["options"],
                affects_calibration=view["affects_calibration"],
                scoring_targets=svc.scoring_params(serial).model_dump(mode="json"),
                profile={
                    "label": prof.label,
                    "characteristics": prof.characteristics.model_dump(mode="json"),
                    "current_mode": mode,
                    "knobs": list(prof.mode(mode)[1].knobs),
                },
            )

        return body()

    @server.tool(structured_output=False)
    def finish_tuning_run(
        run_id: RunId,
        best_capture_id: CaptureId,
        preset_name: PresetName | None = None,
        summary: Annotated[str, Field(max_length=2000)] = "",
    ) -> Any:
        """Finish the run with its best capture; with preset_name, save that capture's settings + lights as a preset."""

        @guarded("finish_tuning_run")
        def body(state: AppState) -> dict[str, Any]:
            res = state.service.finish_run(run_id, best_capture_id, preset_name, summary, ACTOR)
            return _ok(
                record=res.record.model_dump(mode="json"),
                preset_path=str(res.preset_path) if res.preset_path else None,
            )

        return body()

    @server.tool(structured_output=False)
    def run_classic_tune(
        camera_id: CameraId,
        target_mean: Annotated[float, Field(ge=0.1, le=0.9)] | None = None,
        max_steps: Annotated[int, Field(ge=1, le=1000)] = 15,
        white_balance: bool = False,
        mode: ModeName | None = None,
    ) -> Any:
        """Run the built-in non-AI tuner (the camera profile's strategy for the mode) as a baseline."""

        @guarded("run_classic_tune")
        def body(state: AppState) -> dict[str, Any]:
            serial = _ensure_open(state, camera_id)
            res = state.service.tune(
                serial,
                mode,
                max_steps=max_steps,
                target_mean=target_mean,
                white_balance=white_balance,
                operator="agent",
                actor=ACTOR,
            )
            return _ok(**res.to_dict())

        return body()

    @server.tool(structured_output=False)
    def list_presets(camera_id: CameraId | None = None) -> Any:
        """List saved presets (all cameras or one)."""

        @guarded("list_presets")
        def body(state: AppState) -> dict[str, Any]:
            refs = state.service.list_presets(camera_id)
            return _ok(presets=[{"camera": r.serial, "name": r.name} for r in refs])

        return body()

    @server.tool(structured_output=False)
    def get_preset(camera_id: CameraId, name: PresetName) -> Any:
        """Show one preset."""

        @guarded("get_preset", "camera_id", log_call=True)
        def body(state: AppState, camera_id: str) -> dict[str, Any]:
            return _ok(preset=state.service.get_preset(camera_id, name).model_dump(mode="json"))

        return body(camera_id=camera_id)

    @server.tool(structured_output=False)
    def apply_preset(camera_id: CameraId, name: PresetName) -> Any:
        """Apply a preset's settings (through the safety limits) and light levels."""

        @guarded("apply_preset")
        def body(state: AppState) -> dict[str, Any]:
            serial = _ensure_open(state, camera_id)
            res = state.service.apply_preset(serial, name, ACTOR)
            return _ok(
                settings=res.apply.model_dump(mode="json"),
                all_applied=res.apply.ok,
                lights=None if res.lights is None else res.lights.levels,
            )

        return body()

    @server.tool(structured_output=False)
    def get_safety_limits() -> Any:
        """The effective safety limits (read-only; only a human can change them by editing the file)."""

        @guarded("get_safety_limits")
        def body(state: AppState) -> dict[str, Any]:
            return _ok(**state.service.limits_view())

        return body()

    return server


def run_stdio(sim: bool = False) -> None:
    """Start the server on stdio (the only transport camera_lab allows)."""
    create_server(sim=sim).run(transport="stdio")


#: The tool names, for the human-reviewed list and the "no limit-changing tool" test.
TOOL_NAMES: tuple[str, ...] = (
    "list_cameras",
    "get_camera_profile",
    "set_camera_mode",
    "get_camera_settings",
    "set_camera_settings",
    "capture",
    "score_capture",
    "list_lights",
    "set_lights",
    "lights_off",
    "start_tuning_run",
    "finish_tuning_run",
    "run_classic_tune",
    "list_presets",
    "get_preset",
    "apply_preset",
    "get_safety_limits",
)
Transport = Literal["stdio"]
