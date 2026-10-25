"""User-set module: what the camera keeps across a restart — every setting, not a sample
(Derek 2026-10-25: "saving settings correctly and setting user is very important").

1. Finish row (``user_sets``): a test value for every setting camera_lab can write (each
   read back), saved to the user set the camera boots from, a software reboot
   (``DeviceReset``), then kept / lost per setting.
2. Power row (``power_cycle``): the human unplugs the camera ≥ 10 s and plugs it back:
   kept / lost per setting after a real power cut, compared with the software reboot.
3. ``site_path``: exactly what the setup tool does on site — the design mode saved to User
   Set 1 as the boot set ("installed on site"), a restart (the software reboot: step 2
   proved it equals a power cut), and the camera must come back in the design mode.
4. ``restore_user_sets``: everything put back (boot-set cameras: User Set 1's previous
   content and the previous boot set; cameras without a boot-set feature (JAI): User Set 1
   refilled with factory Default), then a restart proves the camera boots as from Default.

Settings the driver sets itself on every open (``OPEN_SETS``: trigger off, free-run frame
rate; the JAI's common exposure and master gain mode) are listed, not tested: a user set's
value for them is never seen through camera_lab.
"""

from __future__ import annotations

import math
from typing import Any

from camera_lab.core.safety import NotAdjustable
from camera_lab.two_d.errors import ConfigurationError, InvalidRequestError
from camera_lab.two_d.fingerprint.common import (
    BOOT_SLOT,
    PERSIST_SLOT,
    FpResult,
    format_family,
)
from camera_lab.two_d.fingerprint.core import CoreMeasurements
from camera_lab.two_d.models import ROI, CameraSettings, CameraSettingsPatch

#: Applied in this order: the format and the ROI change the other ranges; HDR tuning last
#: (it makes exposure and gain read-only on the TRI054S).
FIRST = ("pixel_format", "roi_width", "roi_height", "roi_x", "roi_y")
LAST = ("hdr.enabled",)
SITE_SLOT = "UserSet1"


def flat(s: CameraSettings) -> dict[str, Any]:
    """Every setting as one key → value (camera_lab names; ``wb_*``, ``roi_*`` split out)."""
    out: dict[str, Any] = {
        "exposure_us": s.exposure_us,
        "gain_db": s.gain_db,
        "pixel_format": s.pixel_format,
        "roi_x": s.roi.x,
        "roi_y": s.roi.y,
        "roi_width": s.roi.width,
        "roi_height": s.roi.height,
        "trigger_mode": s.trigger_mode,
    }
    if s.gamma is not None:
        out["gamma"] = s.gamma
    if s.frame_rate_hz is not None:
        out["frame_rate_hz"] = s.frame_rate_hz
    for ch, v in (s.white_balance or {}).items():
        out[f"wb_{ch}"] = v
    out.update(s.advanced)
    return out


def patch_for(key: str, value: Any, now: CameraSettings) -> CameraSettingsPatch:
    """The patch that sets one flat setting."""
    if key.startswith("wb_"):
        return CameraSettingsPatch(white_balance={key[3:]: value})  # type: ignore[dict-item]
    if key.startswith("roi_"):
        r = now.roi.model_dump()
        r[key[4:]] = int(value)
        return CameraSettingsPatch(roi=ROI(**r))
    if key in CameraSettingsPatch.model_fields and key != "advanced":
        return CameraSettingsPatch(**{key: value})
    return CameraSettingsPatch(advanced={key: value})


def same(a: Any, b: Any) -> bool:
    if isinstance(a, int | float) and isinstance(b, int | float):
        return math.isclose(float(a), float(b), rel_tol=0.01, abs_tol=1e-6)
    return bool(a == b)


class UserSetMeasurements(CoreMeasurements):
    """The user-set module."""

    _persist: dict[str, Any]

    # ------------------------------------------------------------ helpers
    def _open_sets(self) -> tuple[str, ...]:
        return tuple(getattr(self.service._open[self.camera_id].camera, "OPEN_SETS", ()))

    def _marker(self, key: str, now: Any, view: Any) -> Any:
        """A valid value different from ``now`` for one setting (None: no other value)."""
        opts = view.options.get(key)
        if opts:
            if key == "pixel_format":  # stay in the sensor's raw family
                opts = [o for o in opts if format_family(o) == format_family(str(now))]
            other = [o for o in opts if o != now]
            return other[0] if other else None
        rng = view.ranges.get(key)
        if rng is None or isinstance(rng, NotAdjustable) or not isinstance(now, int | float):
            return None
        lo, hi = float(rng.min.value), float(rng.max.value)
        if hi <= lo:
            return None
        if key in ("roi_width", "roi_height"):
            return max(int(lo), int(now * 0.75) // 16 * 16)
        if key in ("roi_x", "roi_y"):
            return min(int(hi), 16) if now != 16 else 0
        cand = float(now) * 0.83 if now else lo + 0.2 * (hi - lo)
        if not lo <= cand <= hi or same(cand, now):
            cand = lo + 0.37 * (hi - lo)
        return cand

    def _set_markers(self) -> tuple[dict[str, Any], dict[str, str]]:
        """A test value for every writable setting: (key → value read back, key → why not)."""
        view = self.service.get_settings(self.camera_id)
        keys = list(flat(view.settings))
        order = [k for k in FIRST if k in keys]
        order += [k for k in keys if k not in FIRST and k not in LAST]
        order += [k for k in LAST if k in keys]
        open_sets = self._open_sets()
        written: dict[str, Any] = {}
        skipped: dict[str, str] = {}
        for key in order:
            if key in open_sets:
                skipped[key] = "set by camera_lab every time it opens the camera"
                continue
            view = self.service.get_settings(self.camera_id)
            now = flat(view.settings).get(key)
            want = self._marker(key, now, view)
            if want is None:
                skipped[key] = "no other value to test with"
                continue
            res = self.service.apply(
                self.camera_id, patch_for(key, want, view.settings), self.actor, mode="clamp"
            )
            bad = [r for r in res.results if r.status == "rejected"]
            if bad:
                skipped[key] = f"not writable now: {bad[0].reason}"
                continue
            got = flat(self.service.get_settings(self.camera_id).settings).get(key)
            if same(got, now):
                skipped[key] = "the camera kept its value"
                continue
            written[key] = got
        # a later write may have moved an earlier one (format → ROI, ROI → frame rate...)
        final = flat(self.service.get_settings(self.camera_id).settings)
        return {k: final.get(k) for k in written}, skipped

    def _compare(self, marked: dict[str, Any]) -> tuple[list[str], list[str]]:
        after = flat(self.service.get_settings(self.camera_id).settings)
        kept = [k for k, v in marked.items() if same(after.get(k), v)]
        return kept, [k for k in marked if k not in kept]

    # ------------------------------------------------------------- finish
    def step_user_sets(self) -> FpResult:
        self._require("identity")
        info = self.service.user_set_info(self.camera_id)
        slots = info.get("slots", [])
        boot_feature = bool(info.get("boot_set_feature"))
        if boot_feature and PERSIST_SLOT in slots:
            slot = PERSIST_SLOT
        elif not boot_feature and BOOT_SLOT in slots:
            slot = BOOT_SLOT
        else:
            return self._record(
                FpResult(
                    step="user_sets",
                    status="pass",
                    message=f"user sets {', '.join(slots) or 'none'}: no slot to test with; "
                    "persistence not tested",
                    data=info,
                )
            )
        marked, skipped = self._set_markers()
        self.service.save_user_set(
            self.camera_id, slot, confirm=True, make_default=boot_feature, actor=self.actor
        )
        self._persist = {
            "slot": slot,
            "boot_feature": boot_feature,
            "prev_boot": info.get("boot_set") or "Default",
            "marked": marked,
            "restored": False,
        }
        try:
            restart = self.service.reboot_camera(self.camera_id, confirm=True, actor=self.actor)
        except ConfigurationError as exc:  # no DeviceReset: the power row still tests it
            restart = {"error": str(exc)}
            kept: list[str] = []
            lost: list[str] = []
        else:
            kept, lost = self._compare(marked)
        self._persist["reboot"] = {"kept": kept, "lost": lost}
        self._persist["volatile"] = self._volatile_marker(marked)
        msg = (
            f"{len(marked)} settings saved to {slot}; after a software reboot "
            f"({restart.get('verified_by', 'not possible')}): kept {len(kept)}, lost "
            f"{len(lost)}"
            + (f" ({', '.join(lost)})" if lost else "")
            + f"; {len(skipped)} not tested"
        )
        return self._record(
            FpResult(
                step="user_sets",
                status="pass",
                message=msg,
                data={
                    **info,
                    "slot": slot,
                    "settings": {
                        k: {"test_value": v, "after_reboot": k in kept} for k, v in marked.items()
                    },
                    "not_tested": skipped,
                    "persistence": {"kept": kept, "lost": lost, "slot": slot},
                    "restart": restart,
                },
            )
        )

    def _volatile_marker(self, marked: dict[str, Any]) -> tuple[str, Any] | None:
        """A live change that is NOT saved: a real power cut reverts it, so it proves the
        human unplugged the camera (the JAI has no uptime counter)."""
        for key in ("exposure_us", "gain_db"):
            if key not in marked:
                continue
            now = self.service.get_settings(self.camera_id).settings
            want = float(marked[key]) * 0.9 if marked[key] else 1.0
            self.service.apply(self.camera_id, patch_for(key, want, now), self.actor, mode="clamp")
            got = flat(self.service.get_settings(self.camera_id).settings).get(key)
            if not same(got, marked[key]):
                return key, got
        return None

    # -------------------------------------------------------------- power
    def step_power_cycle(self) -> FpResult:
        self._require("user_sets")
        p = getattr(self, "_persist", None)
        if not p:
            return self._record(
                FpResult(step="power_cycle", status="skipped", message="no user set was tested")
            )
        info = self.service.reconnect_camera(self.camera_id)
        up = info.get("uptime_after_s")
        vol = p.get("volatile")
        still = vol is not None and same(
            flat(self.service.get_settings(self.camera_id).settings).get(vol[0]), vol[1]
        )
        if still or (isinstance(up, int | float) and up > 600):
            return self._record(
                FpResult(
                    step="power_cycle",
                    status="fail",
                    message="the camera was not power-cycled (a setting camera_lab changed "
                    "without saving is still there). Unplug it for at least 10 s, plug it back, "
                    "wait for the link light, then Retry",
                )
            )
        kept, lost = self._compare(p["marked"])
        reboot = p.get("reboot", {})
        identical = sorted(kept) == sorted(reboot.get("kept", [])) and bool(reboot.get("kept"))
        p["power"] = {"kept": kept, "lost": lost, "identical_to_reboot": identical}
        msg = (
            f"after unplugging: kept {len(kept)}, lost {len(lost)}"
            + (f" ({', '.join(lost)})" if lost else "")
            + (
                "; identical to the software reboot"
                if identical
                else "; DIFFERENT from the software reboot"
            )
            + (f"; uptime {up:.0f} s" if isinstance(up, int | float) else "")
        )
        return self._record(
            FpResult(
                step="power_cycle",
                status="pass",
                message=msg,
                data={**p["power"], "uptime_after_s": up},
            )
        )

    def step_site_path(self) -> FpResult:
        """The setup tool's "installed on site" save, a restart, the design mode back."""
        self._require("user_sets")
        p = getattr(self, "_persist", None) or {}
        boot_feature = bool(p.get("boot_feature"))
        if boot_feature:  # keep User Set 1's content to put it back
            try:
                self.service.load_user_set(self.camera_id, SITE_SLOT)
            except InvalidRequestError:  # never saved: nothing to put back
                p["site_slot_before"] = None
            else:
                p["site_slot_before"] = flat(self.service.get_settings(self.camera_id).settings)
        self.service.reset_camera(self.camera_id, self.mode, self.actor)
        self.service.save_user_set(
            self.camera_id, SITE_SLOT, confirm=True, make_default=boot_feature, actor=self.actor
        )
        p["site_saved"] = True
        try:
            restart = self.service.reboot_camera(self.camera_id, confirm=True, actor=self.actor)
        except ConfigurationError as exc:
            return self._record(
                FpResult(step="site_path", status="skipped", message=f"cannot restart: {exc}")
            )
        mism = self.service.mode_mismatches(self.camera_id, self.mode)
        status = "pass" if not mism else "fail"
        msg = (
            f"saved the design mode {self.mode} to {SITE_SLOT} as on site, restarted "
            f"({restart.get('verified_by')}): "
            + (
                "the camera came back in the design mode"
                if not mism
                else "NOT in the design mode after the restart: " + "; ".join(mism)
            )
        )
        if mism:
            msg += ". The user set does not hold the whole mode: the site must reset after boot"
        return self._record(
            FpResult(
                step="site_path",
                status=status,  # type: ignore[arg-type]
                message=msg,
                data={"mode": self.mode, "mismatches": mism, "restart": restart},
            )
        )

    def step_restore_user_sets(self) -> FpResult:
        """Everything back, then a restart proves the camera boots as from factory Default."""
        p = getattr(self, "_persist", None)
        if not p:
            return self._record(
                FpResult(step="restore_user_sets", status="skipped", message="nothing to restore")
            )
        done = self._restore_slots()
        try:
            self.service.reboot_camera(self.camera_id, confirm=True, actor=self.actor)
        except ConfigurationError as exc:
            return self._record(
                FpResult(
                    step="restore_user_sets",
                    status="pass",
                    message=f"{done}; not checked by a restart ({exc})",
                )
            )
        booted = flat(self.service.get_settings(self.camera_id).settings)
        self.service.load_user_set(self.camera_id, "Default")
        default = flat(self.service.get_settings(self.camera_id).settings)
        open_sets = self._open_sets()
        diff = [
            k
            for k in sorted(set(booted) | set(default))
            if k not in open_sets and not same(booted.get(k), default.get(k))
        ]
        expect_default = (not p["boot_feature"]) or p.get("prev_boot") == "Default"
        if expect_default and diff:
            return self._record(
                FpResult(
                    step="restore_user_sets",
                    status="fail",
                    message=f"{done}; but after a restart the camera does NOT boot as from "
                    f"factory Default: {', '.join(diff)} differ",
                    data={"differences": diff},
                )
            )
        return self._record(
            FpResult(
                step="restore_user_sets",
                status="pass",
                message=f"{done}; after a restart the camera boots "
                + ("as from factory Default" if expect_default else f"from {p['prev_boot']}"),
                data={"differences": diff},
            )
        )

    def _restore_slots(self) -> str:
        p = self._persist
        if p.get("restored"):
            return "already restored"
        if p["boot_feature"]:
            before = p.get("site_slot_before")
            if before is not None:
                now = self.service.get_settings(self.camera_id).settings
                for key in (*FIRST, *(k for k in before if k not in FIRST)):
                    if key in before and key not in self._open_sets():
                        self.service.apply(
                            self.camera_id,
                            patch_for(key, before[key], now),
                            self.actor,
                            mode="clamp",
                        )
                        now = self.service.get_settings(self.camera_id).settings
                self.service.save_user_set(
                    self.camera_id, SITE_SLOT, confirm=True, make_default=False, actor=self.actor
                )
            self.service.set_boot_user_set(
                self.camera_id, p["prev_boot"], confirm=True, actor=self.actor
            )
            text = f"boot set back to {p['prev_boot']}" + (
                f", {SITE_SLOT} back to its previous content" if before is not None else ""
            )
        else:
            self.service.refill_user_set_with_default(
                self.camera_id, BOOT_SLOT, confirm=True, actor=self.actor
            )
            text = f"{BOOT_SLOT} refilled with factory Default"
        p["restored"] = True
        return text

    def restore_if_pending(self) -> str | None:
        """A fingerprint left after the finish row (window closed): put the user sets back."""
        p = getattr(self, "_persist", None)
        if not p or p.get("restored"):
            return None
        return self._restore_slots()
