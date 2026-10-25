"""The rows the human works through: one per bench setup, built from the sensor type
(core, colour, HDR modules) and the camera model's light recipe."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import TYPE_CHECKING

from camera_lab.two_d.errors import InvalidRequestError

if TYPE_CHECKING:
    from camera_lab.two_d.profiles import FpChannelStage
from camera_lab.two_d.fingerprint.base import SessionBase
from camera_lab.two_d.fingerprint.common import (
    BOOT_SLOT,
    CHANNEL_PREFIX,
    FP_SETUPS,
    LENS_SETUPS,
    LENS_STEPS,
    LIGHT_SETUPS,
    PERSIST_SLOT,
    REPEATS,
    STAGE_STEPS,
    FpSetup,
    FpStep,
    StepPlan,
)

PERSIST_CHECK = "OK to write test values to {slot} and restart the camera"


class PlanMixin(SessionBase):
    """Builds :attr:`plan`."""

    def _build_plan(self) -> dict[FpSetup, StepPlan]:
        """The human's setups (what is on the lens, the iris, the light), each with the
        measurements one Run makes there. Inactive setups are not shown."""
        r = self.recipe
        # every row the human returns to runs at the reference: the recipe's, else the bench
        # light at its brightest and the iris fully open (an end stop, Derek 2026-10-24)
        ref = self._reference()
        picked = self.lens_spec is not None
        lens_check: tuple[str, ...] = (
            (f"Lens: {self.lens_label} on the camera",) if picked else ("Lens picked in the list",)
        )
        ref_light: tuple[str, ...] = (
            self._iris_text(ref.f_number) if picked else "Iris fully open",
            f"Bench light on dimmer {ref.dimmer}",
        )
        at_ref: list[FpStep] = []
        others: list[tuple[FpStep, FpChannelStage]] = []
        if r is not None:
            for i, st in enumerate(r.channel_stages):
                if (st.dimmer, st.f_number) == (r.reference.dimmer, r.reference.f_number):
                    at_ref.append(STAGE_STEPS[i])
                else:
                    others.append((STAGE_STEPS[i], st))
            if len(others) > len(LIGHT_SETUPS):
                raise InvalidRequestError(
                    f"the recipe needs {len(others)} light changes; at most "
                    f"{len(LIGHT_SETUPS)} are supported"
                )
        ref_names = self._channel_names(at_ref)
        # Modules by sensor type (Derek 2026-10-22): core for every camera; colour planes for
        # Bayer and prism sensors; HDR channel rows only where the profile says HDR.
        colour: tuple[FpStep, ...] = ("colour",) if self.colour != "mono" else ()
        plan: dict[FpSetup, StepPlan] = {
            "cap": StepPlan(
                "cap",
                "Lens cap on",
                "Put the LENS CAP on, iris and light at the reference, and check the live image "
                f"is black. One Run: camera identity, then the darks {REPEATS}× (minutes). Focus "
                "is not needed for the fingerprint.",
                (
                    *lens_check,
                    *ref_light,
                    "Lens cap on",
                    "Live image is black",
                    "Focus: not needed",
                ),
                ("identity", "dark"),
                f_number=ref.f_number if picked else None,
            ),
            "diffuser": StepPlan(
                "diffuser",
                "Diffuser on" + (f", iris f/{ref.f_number:g}" if picked else ""),
                "Take the cap off and put the OPAL DIFFUSER on the lens: the live image evenly "
                "white with no edges. Keep it still. One Run: timing, the flat field"
                + (", the colour planes" if colour else "")
                + (f", HDR ch {ref_names}" if ref_names else "")
                + f" and noise, each {REPEATS}× (10–15 minutes). camera_lab changes the exposure "
                "while it measures, so the live view brightness jumps: do not touch the lens or "
                "the light until the row says pass or STOP.",
                (
                    "Lens cap off",
                    "Opal diffuser on the lens, covering the whole view, held still",
                    *ref_light,
                ),
                ("timing", "flat", *colour, *at_ref, "noise_gain"),
                f_number=ref.f_number if picked else None,
            ),
        }
        self._light_changes = True  # the lens row closes the iris (others / HDR discovery too)
        for n, setup in enumerate(LIGHT_SETUPS):
            if r is not None and n < len(others):
                step, st = others[n]
                dimmer = f", dimmer {st.dimmer}" if st.dimmer != r.reference.dimmer else ""
                names = self._channel_names([step])
                plan[setup] = StepPlan(
                    setup,
                    f"Iris f/{st.f_number:g}{dimmer} (HDR ch {names})",
                    f"Keep the diffuser on. Set the iris to f/{st.f_number:g} and the bench light "
                    f"to dimmer {st.dimmer}. camera_lab measures the light first and stops if it "
                    f"does not match; then HDR ch {names} {REPEATS}×. If it cannot pass, Skip "
                    "records that in the report.",
                    (
                        f"Bench light on dimmer {st.dimmer}",
                        self._iris_text(st.f_number),
                        "Diffuser still on the lens",
                    ),
                    (step,),
                    can_skip=True,
                    f_number=st.f_number,
                )
            elif r is None and n == 0 and self.hdr_capable:
                plan[setup] = StepPlan(
                    setup,
                    "HDR channels (discovery)",
                    "No light recipe for this camera model yet. Each channel reports how much "
                    "less or more light it needs (a factor and f-stops): set the iris to the "
                    "marked stop it names, pick that stop in the list, then Retry. Measured "
                    "channels are kept; the factors become this model's recipe.",
                    (
                        "Diffuser still on the lens",
                        "The stop picked in the list is set on the iris",
                    ),
                    ("channels_a",),
                    can_skip=True,
                    iris_pick=True,
                )
            else:
                plan[setup] = StepPlan(setup, "Not used", "Not used for this camera.", active=False)
        # one row per marked stop between the end stops, the iris turning one way (Derek
        # 2026-10-25: every stop), then the low-light row at the closed end stop
        marks = sorted(self.lens_spec.f_stops) if self.lens_spec is not None else []
        closed = marks[-1] if len(marks) > 1 else None
        middle = [f for f in marks if f not in (ref.f_number, closed)]
        for i, setup in enumerate(LENS_SETUPS):
            if i < len(middle):
                f = middle[i]
                plan[setup] = StepPlan(
                    setup,
                    f"Iris f/{f:g}",
                    f"Keep the diffuser on. Set the iris to the f/{f:g} mark. camera_lab checks "
                    "the light against the lens's stops, then measures the light this stop "
                    "passes, vignetting and colour shading against fully open (prism cameras "
                    "are rated for one aperture). Skip only if the lens has no iris.",
                    (self._iris_text(f), "Diffuser still on the lens"),
                    (LENS_STEPS[i],),
                    can_skip=True,
                    f_number=f,
                )
            else:
                plan[setup] = StepPlan(setup, "Not used", "Not used for this lens.", active=False)
        plan["low_light"] = StepPlan(
            "low_light",
            f"Iris fully closed (f/{closed:g}): low light" if closed else "Low light",
            "Keep the diffuser on and turn the iris to its closed end stop. camera_lab checks "
            "the light, then measures the darkest signals (from about one read noise up), gain "
            "at a fixed exposure, the gain steps the bright row could not reach, and frame "
            "averaging (minutes). Skip only if the lens has no iris.",
            (
                self._iris_text(closed) if closed else "Iris fully closed (end stop)",
                "Diffuser still on the lens",
            ),
            ("low_light",),
            can_skip=True,
            f_number=closed,
            active=closed is not None or not picked,
        )
        info = self.service.user_set_info(self.camera_id)
        slots = info.get("slots", [])
        persist = (
            PERSIST_SLOT if info.get("boot_set_feature") and PERSIST_SLOT in slots else BOOT_SLOT
        )
        persist_check = PERSIST_CHECK.format(slot=persist) + (
            ""
            if persist == PERSIST_SLOT
            else f"; {BOOT_SLOT} is then refilled with factory Default"
        )
        back = ref_light
        plan["finish"] = StepPlan(
            "finish",
            "Iris back: finish",
            "Set the iris and the light back as in the diffuser row; keep the diffuser on. "
            "camera_lab checks the light matches the flat step, tests what survives a restart "
            f"(test values in {persist}, the camera reboots, ≈ 30 s) and writes the fingerprint. "
            "Remove the diffuser afterwards.",
            (*back, "Diffuser still on the lens", persist_check),
            ("light_back", "user_sets", "report"),
            f_number=ref.f_number if picked else None,
        )
        n = 0
        for setup in FP_SETUPS:
            p = plan[setup]
            if p.active:
                n += 1
                plan[setup] = replace(p, label=f"{n}. {p.label}")
        return plan

    def _channel_names(self, steps: Iterable[FpStep]) -> str:
        r = self.recipe
        if r is None:
            return ""
        chans = [
            c.removeprefix(CHANNEL_PREFIX)
            for s in steps
            for c in r.channel_stages[STAGE_STEPS.index(s)].channels
        ]
        return ", ".join(chans)
