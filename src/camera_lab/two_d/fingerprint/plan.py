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
    CHANNEL_PREFIX,
    FP_SETUPS,
    LIGHT_SETUPS,
    REPEATS,
    STAGE_STEPS,
    FpSetup,
    FpStep,
    StepPlan,
)


class PlanMixin(SessionBase):
    """Builds :attr:`plan`."""

    def _build_plan(self) -> dict[FpSetup, StepPlan]:
        """The human's setups (what is on the lens, the iris, the light), each with the
        measurements one Run makes there. Inactive setups are not shown."""
        r = self.recipe
        lens_check: tuple[str, ...] = ()
        if r is not None:
            ref = r.reference
            ref_light: tuple[str, ...] = (
                self._iris_text(ref.f_number),
                f"Bench light on dimmer {ref.dimmer}",
            )
            lens_check = (f"Lens: {self.lens_label} on the camera",)
        else:
            ref_light = ("Iris fully open", "Bench light on, written in the Lens / iris box")
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
            ),
            "diffuser": StepPlan(
                "diffuser",
                "Diffuser on" + (f", iris f/{r.reference.f_number:g}" if r is not None else ""),
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
            ),
        }
        self._light_changes = bool(others) or (r is None and self.hdr_capable)
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
                )
            elif r is None and n == 0 and self.hdr_capable:
                plan[setup] = StepPlan(
                    setup,
                    "HDR channels (discovery)",
                    "No light recipe for this camera model yet. Each channel reports how much "
                    "less or more light it needs (a factor and f-stops): change the light as "
                    "asked, write it in the Lens / iris box, then Retry. Measured channels are "
                    "kept; the factors become this model's recipe.",
                    ("Diffuser still on the lens", "Lens / iris box matches the light now"),
                    ("channels_a",),
                    can_skip=True,
                )
            else:
                plan[setup] = StepPlan(setup, "Not used", "Not used for this camera.", active=False)
        back = (*ref_light, "Diffuser still on the lens") if self._light_changes else ()
        plan["finish"] = StepPlan(
            "finish",
            "Iris back open: finish" if self._light_changes else "Finish",
            (
                "Set the iris back fully open and the light to the reference; keep the diffuser "
                "on. camera_lab checks the light matches the flat step, reads the user sets and "
                "writes the fingerprint. Remove the diffuser afterwards."
            )
            if self._light_changes
            else "Run to read the user sets and write the fingerprint; then remove the diffuser.",
            back,
            ("light_back", "user_sets", "report"),
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
