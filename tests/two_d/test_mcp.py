"""MCP server tests: every tool (valid / boundary / invalid), error shape, no limit
changes, path-like input, step logging, shutdown, and stdout cleanliness."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp import Client

from camera_lab.core.lights import SimulatedLight
from camera_lab.core.mcp_server import TOOL_NAMES, create_server, workflow_text
from camera_lab.core.safety import load_safety_limits
from camera_lab.two_d.camera_config import load_cameras_config
from camera_lab.two_d.service import TwoDService

pytestmark = pytest.mark.anyio
SIM = "SIM-0001"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def svc(make_service: Callable[..., TwoDService]) -> TwoDService:
    return make_service(cameras=load_cameras_config())


@pytest.fixture
def server(svc: TwoDService) -> Any:
    return create_server(sim=True, service_factory=lambda: svc)


def body(result: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(result.content[0].text)
    return data


async def call(client: Client, tool: str, **args: Any) -> dict[str, Any]:
    return body(await client.call_tool(tool, args))


def assert_error(data: dict[str, Any], code: str) -> None:
    assert data["ok"] is False, data
    assert set(data["error"]) == {"code", "message"}
    assert data["error"]["code"] == code, data
    assert "Traceback" not in data["error"]["message"]


# ------------------------------------------------------------ tool surface
async def test_tool_list_is_exactly_the_reviewed_surface(server: Any) -> None:
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == set(TOOL_NAMES)
    for name, tool in tools.items():
        assert "limit" not in name or name == "get_safety_limits"
        props = tool.input_schema.get("properties", {})
        assert not any("limit" in p or "safety" in p for p in props), (name, props)
        assert tool.description
    assert tools["get_safety_limits"].input_schema.get("properties", {}) == {}
    cap = tools["capture"].input_schema["properties"]["count"]
    assert (cap.get("minimum"), cap.get("maximum")) == (1, 5)


async def test_instructions(server: Any, svc: TwoDService) -> None:
    text = workflow_text(svc)
    for phrase in (
        "start_tuning_run",
        "finish_tuning_run",
        "lights_off",
        "Rejections are normal",
        "cannot change",
        "target 0.45",
        "get_camera_profile",
    ):
        assert phrase in text
    assert server.instructions == text


# --------------------------------------------------------------- the loop
async def test_full_agent_run(server: Any, svc: TwoDService) -> None:
    async with Client(server) as c:
        cams = await call(c, "list_cameras", include_sim=True)
        assert cams["ok"] and cams["cameras"][0]["serial"] == SIM
        assert (await call(c, "list_cameras"))["cameras"] == []  # sim hidden unless asked
        prof = await call(c, "get_camera_profile", camera_id="sim")
        assert prof["profile"]["default_mode"] == "linear"
        start = await call(c, "start_tuning_run", camera_id="sim", goal="test run", max_steps=10)
        assert start["ok"] and start["step_budget"] == 10, start
        assert start["scoring_targets"]["target_mean"] == 0.45
        assert start["ranges"]["gain_db"]["max"] == 24  # camera range (limits policy)
        assert start["working_range"]["gain_db"]["max"] == 18  # guidance for the agent
        assert start["profile"]["knobs"] == ["exposure_us", "gain_db", "white_balance"]
        run_id = start["run_id"]
        rej = await call(
            c, "set_camera_settings", camera_id="sim", changes={"gain_db": 30, "exposure_us": 20000}
        )
        assert rej["ok"] and rej["all_applied"] is False
        by = {r["field"]: r for r in rej["results"]}
        assert "camera max 24.0" in by["gain_db"]["reason"]
        assert by["exposure_us"]["status"] == "applied"
        ids = []
        for _ in range(3):
            res = await c.call_tool("capture", {"camera_id": "sim", "run_id": run_id})
            assert [x.type for x in res.content] == ["text", "image"]
            assert res.content[1].mime_type == "image/jpeg"
            ids.append(body(res)["captures"][0]["capture_id"])
        sc = await call(c, "score_capture", capture_id=ids[0])
        assert sc["scores"]["composite"] >= 0
        fin = await call(
            c,
            "finish_tuning_run",
            run_id=run_id,
            best_capture_id=ids[-1],
            preset_name="agent_best",
            summary="done",
        )
        assert fin["ok"] and fin["preset_path"].endswith("SIM-0001/agent_best.yaml")
        assert fin["record"]["method"] == "agent" and fin["record"]["operator"] == "agent"
    steps = [json.loads(line) for line in _steps(run_id).splitlines()]
    tools = [s["tool"] for s in steps]
    assert tools[0] == "start_tuning_run" and tools.count("capture") == 3
    assert "set_camera_settings" in tools and "finish_tuning_run" in tools
    assert all(s["actor"] == "agent" for s in steps[1:])


def _steps(run_id: str) -> str:
    from camera_lab.core.paths import two_d_runs_root

    return (two_d_runs_root() / run_id / "steps.jsonl").read_text()


async def test_read_calls_logged_in_active_run(server: Any) -> None:
    async with Client(server) as c:
        start = await call(c, "start_tuning_run", camera_id=SIM, goal="g", max_steps=5)
        await call(c, "get_camera_settings", camera_id=SIM)
        await call(c, "get_camera_profile", camera_id=SIM)
    tools = [json.loads(x)["tool"] for x in _steps(start["run_id"]).splitlines()]
    assert "get_camera_settings" in tools and "get_camera_profile" in tools


async def test_settings_mode_lights_presets(server: Any, sim_light: SimulatedLight) -> None:
    async with Client(server) as c:
        view = await call(c, "get_camera_settings", camera_id=SIM)
        assert view["ok"] and "roi_width" in view["affects_calibration"]
        mode = await call(c, "set_camera_mode", camera_id=SIM, mode="hdr")
        assert mode["ok"] and mode["result"]["settings"]["advanced"]["hdr.enabled"] == "on"
        assert (await call(c, "get_camera_settings", camera_id=SIM))["mode"] == "hdr"
        assert_error(await call(c, "set_camera_mode", camera_id=SIM, mode="nope"), "not_found")
        lights = await call(c, "list_lights")
        assert {ch["id"] for ch in lights["channels"]} == {"white_ring", "white_bar", "red", "blue"}
        lit = await call(c, "set_lights", levels={"white_ring": 50, "red": 95})
        assert lit["ok"] and lit["all_applied"] is False and lit["levels"]["white_ring"] == 50
        assert sim_light.get_levels()["red"] == 0
        assert (await call(c, "lights_off"))["levels"]["white_ring"] == 0
        cap = await c.call_tool("capture", {"camera_id": SIM})
        cid = body(cap)["captures"][0]["capture_id"]
        tune = await call(c, "run_classic_tune", camera_id=SIM, mode="linear", max_steps=12)
        assert tune["ok"] and tune["converged"] and tune["strategy"] == "linear"
        assert (await call(c, "list_presets", camera_id=SIM))["presets"] == []
        assert_error(await call(c, "get_preset", camera_id=SIM, name="nothere"), "not_found")
        assert_error(await call(c, "apply_preset", camera_id=SIM, name="nothere"), "not_found")
        assert cid
        limits = await call(c, "get_safety_limits")
        assert limits["limits"]["mcp"]["max_capture_count"] == 5


# --------------------------------------------------------- bounds/errors
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("capture", {"camera_id": SIM, "count": 6}),
        ("capture", {"camera_id": SIM, "count": 0}),
        ("capture", {"camera_id": SIM, "note": "x" * 501}),
        ("capture", {"camera_id": "../etc"}),
        ("capture", {"camera_id": SIM, "run_id": "../../x"}),
        ("score_capture", {"capture_id": "../../etc/passwd"}),
        ("score_capture", {"capture_id": "20261021-101500_SIM-0001_run/1"}),
        ("get_preset", {"camera_id": SIM, "name": "../x"}),
        ("set_lights", {"levels": {"red": 150}}),
        ("set_lights", {"levels": {}}),
        ("start_tuning_run", {"camera_id": SIM, "goal": "x" * 501, "max_steps": 5}),
        ("start_tuning_run", {"camera_id": SIM, "goal": "g", "max_steps": 0}),
        ("run_classic_tune", {"camera_id": SIM, "target_mean": 0.95}),
        ("finish_tuning_run", {"run_id": "x", "best_capture_id": "y"}),
        ("set_camera_settings", {"camera_id": SIM, "changes": {"gain_db": "loud"}}),
        ("set_camera_settings", {"camera_id": SIM, "changes": {"advanced": {"BAD": 1}}}),
        ("set_camera_mode", {"camera_id": SIM, "mode": "../x"}),
        ("list_cameras", {"include_sim": "maybe"}),
    ],
)
async def test_invalid_arguments_rejected_with_error_shape(
    server: Any, tool: str, args: dict[str, Any]
) -> None:
    async with Client(server) as c:
        res = await c.call_tool(tool, args)
    assert res.is_error
    assert_error(body(res), "invalid_argument")


async def test_unknown_tool(server: Any) -> None:
    async with Client(server) as c:
        res = await c.call_tool("set_safety_limits", {"gain_db": 99})
    assert_error(body(res), "unknown_tool")


async def test_semantic_errors(server: Any) -> None:
    async with Client(server) as c:
        assert_error(
            await call(c, "start_tuning_run", camera_id=SIM, goal="g", max_steps=41),
            "rejected_by_limits",
        )
        assert_error(await call(c, "get_camera_settings", camera_id="U330439"), "camera_not_found")
        assert_error(
            await call(
                c,
                "finish_tuning_run",
                run_id="20261021-101500_SIM-0001_x",
                best_capture_id="20261021-101500_SIM-0001_x/0001",
            ),
            "not_found",
        )
        assert_error(
            await call(c, "score_capture", capture_id="20261021-101500_SIM-0001_x/0001"),
            "not_found",
        )
        start = await call(c, "start_tuning_run", camera_id=SIM, goal="g", max_steps=1)
        await c.call_tool("capture", {"camera_id": SIM, "run_id": start["run_id"]})
        assert_error(
            await call(c, "set_camera_settings", camera_id=SIM, changes={"gain_db": 1}),
            "step_budget_exhausted",
        )


async def test_capture_count_follows_limits_file(
    make_service: Callable[..., TwoDService], tmp_path: Path
) -> None:
    data = yaml.safe_load(Path(load_safety_limits().source).read_text())
    data["mcp"]["max_capture_count"] = 2
    p = tmp_path / "safety_limits.yaml"
    p.write_text(yaml.safe_dump(data))
    svc = make_service()
    svc.limits = load_safety_limits(p)
    async with Client(create_server(sim=True, service_factory=lambda: svc)) as c:
        assert_error(await call(c, "capture", camera_id=SIM, count=3), "invalid_request")
        ok = body(await c.call_tool("capture", {"camera_id": SIM, "count": 2}))
        assert len(ok["captures"]) == 2


async def test_internal_errors_hide_details(
    server: Any, svc: TwoDService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise RuntimeError("secret /home/dvs/path")

    monkeypatch.setattr(svc, "list_lights", boom)
    async with Client(server) as c:
        data = await call(c, "list_lights")
    assert_error(data, "internal_error")
    assert "secret" not in data["error"]["message"]


async def test_shutdown_closes_cameras_and_turns_lights_off(
    server: Any, svc: TwoDService, sim_light: SimulatedLight
) -> None:
    async with Client(server) as c:
        await call(c, "set_lights", levels={"white_ring": 40})
        await call(c, "get_camera_settings", camera_id=SIM)
        assert svc.is_open(SIM)
    assert not svc.is_open(SIM)
    assert sim_light.get_levels()["white_ring"] == 0


async def test_camera_lock_owner_is_mcp(
    server: Any, make_service: Callable[..., TwoDService]
) -> None:
    from camera_lab.two_d.errors import CameraBusyError

    async with Client(server) as c:
        await call(c, "get_camera_settings", camera_id=SIM)
        other = make_service()
        with pytest.raises(CameraBusyError, match="mcp"):
            other.open(SIM, "gui")


# ------------------------------------------------------ stdout cleanliness
def test_stdout_is_only_json_rpc(tmp_roots: Path) -> None:
    exe = Path(sys.executable).with_name("camera-lab")
    msgs = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "capture", "arguments": {"camera_id": "sim"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "set_lights", "arguments": {"levels": {"red": 20}}},
        },
    ]
    proc = subprocess.Popen(
        [str(exe), "mcp", "--sim"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    lines = []
    for m in msgs:
        proc.stdin.write(json.dumps(m) + "\n")
        proc.stdin.flush()
        if "id" in m:  # wait for the reply before the next request
            lines.append(proc.stdout.readline())
    proc.stdin.close()
    lines.extend(proc.stdout.read().splitlines())  # anything else the server prints
    proc.wait(timeout=60)
    stderr = proc.stderr.read()
    lines = [line for line in lines if line.strip()]
    assert len(lines) == 4  # exactly one response per request, nothing else
    for line in lines:
        msg = json.loads(line)  # every stdout line is a JSON-RPC message
        assert msg["jsonrpc"] == "2.0" and "id" in msg and ("result" in msg or "error" in msg)
    assert "lights_all_off" in stderr and "mcp_server_stopped" in stderr
    assert proc.returncode == 0
