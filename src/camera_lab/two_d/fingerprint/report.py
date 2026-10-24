"""The fingerprint files: fingerprint.yaml, report.md and the master dark / flat."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import yaml

from camera_lab.core.paths import safe_join, validate_serial

if TYPE_CHECKING:
    pass
from camera_lab.two_d.fingerprint.base import SessionBase
from camera_lab.two_d.fingerprint.common import (
    FP_STEPS,
    FpResult,
    fingerprints_root,
)


class ReportMixin(SessionBase):
    """Writes the report."""

    def _folder(self) -> Path:
        base = safe_join(self.root or fingerprints_root(), validate_serial(self.camera_id))
        stamp = self.created.strftime("%Y%m%dT%H%M%SZ")
        folder = safe_join(base, stamp)
        n = 1
        while folder.exists():
            folder = safe_join(base, f"{stamp}-{n}")
            n += 1
        return folder

    def step_report(self) -> FpResult:
        self._require(*(m for m in self._planned() if m != "report"))
        folder = self._folder()
        folder.mkdir(parents=True)
        ident = self.results["identity"].data
        doc: dict[str, Any] = {
            "schema_version": 1,
            "camera": {k: ident.get(k) for k in ("model", "serial", "firmware", "vendor")},
            "created_utc": self.created.isoformat(),
            "scope": {
                "camera_only (carries over to the machine)": [
                    "identity",
                    "timing",
                    "dark",
                    "photon_transfer",
                    "response",
                    "channels",
                    "noise_gain",
                    "user_sets",
                ],
                "lens_and_installation (re-measure on the machine)": [
                    "flat.vignetting_corner_ratio",
                    "master_flat",
                ],
            },
            **{s: self.results[s].data for s in FP_STEPS if s in self.results and s != "report"},
            "hdr_channels": dict(self._channels),
            "setups": {
                s: r.model_dump(mode="json", exclude={"data"})
                for s, r in self.setup_results.items()
            },
            "recipe": self.recipe.model_dump(mode="json") if self.recipe is not None else None,
        }
        (folder / "fingerprint.yaml").write_text(
            yaml.safe_dump(json.loads(json.dumps(doc, default=str)), sort_keys=False)
        )
        for name, arr in self._arrays.items():
            np.savez_compressed(folder / f"{name}.npz", data=arr)
        lines = [f"# Camera fingerprint {self.camera_id} ({self.created:%Y-%m-%d %H:%M} UTC)", ""]
        lines += [
            f"- **Model:** {ident.get('model')}",
            f"- **Serial:** {ident.get('serial')}",
            f"- **Firmware:** {ident.get('firmware') or 'unknown'}"
            + (
                f" (device version {ident['device_version']})"
                if ident.get("device_version")
                else ""
            ),
            f"- **Lens / iris:** {ident.get('lens') or 'not stated'}",
            f"- **Conditions:** gain {ident.get('conditions', {}).get('gain_db')} dB, black level "
            f"{ident.get('conditions', {}).get('black_level')}",
            "",
        ]
        lines += [f"- **{r.step}** ({r.status}): {r.message}" for r in self.results.values()]
        snr = self.results["flat"].data.get("response", {}).get("snr", [])
        if snr:
            lines += [
                "",
                "SNR across the range (flat sweep):",
                "",
                "| signal DN | SNR |",
                "|---|---|",
            ]
            lines += [f"| {sig:.6g} | {val:.1f} |" for sig, val in snr]
        colour = self.results.get("colour")
        if colour is not None and colour.status == "pass":
            lines += [
                "",
                "| colour plane | signal DN | rel. to green | temporal σ DN | SNR | PRNU % |",
                "|---|---|---|---|---|---|",
            ]
            lines += [
                f"| {c['name']} | {c['signal_dn']:.6g} | {c['relative_to_green']:.3f} | "
                f"{c['temporal_sigma_dn']:.4g} | {c['snr']:.1f} | {c['prnu_pct']:.2f} |"
                for c in colour.data["planes"]
            ]
            imb = colour.data.get("green_imbalance_pct")
            if imb is not None:
                lines.append(f"\nBayer green imbalance (Gr − Gb): {imb:+.2f} %")
        if self.durations:
            lines += ["", "| setup | measurements (seconds) | total |", "|---|---|---|"]
            total = 0.0
            for setup, ms in self.durations.items():
                t = sum(ms.values())
                total += t
                lines.append(
                    f"| {setup} | "
                    + ", ".join(f"{k} {v:g}" for k, v in ms.items())
                    + f" | {t:.0f} s |"
                )
            lines.append(f"| **all** | | **{total / 60:.1f} min** |")
        pipe = self.results.get("timing")
        if pipe is not None and "pipeline" in pipe.data:
            pp = pipe.data["pipeline"]
            lines += [
                "",
                f"Image pipeline ({pp['pixel_format']}, {pp['frame_mb']:.1f} MB/frame, "
                f"{pp['exposure_us'] / 1000:.1f} ms exposure): a frame every "
                f"{pp['frame_interval_ms']:.0f} ms; {pp['process_ms']:.0f} ms on {pp['pc']} to a "
                "usable image (debayer / float).",
            ]
        chans = self._channels
        if chans:
            lines += [
                "",
                "| HDR channel | system gain DN/e- | read noise e- | full well e- | linearity % "
                "| lens / iris |",
                "|---|---|---|---|---|---|",
            ]
            for name, ch in chans.items():
                pt = ch["photon_transfer"]
                lines.append(
                    f"| {name} | {pt['system_gain_dn_per_e']:.4f} | {pt['read_noise_e']:.2f} | "
                    f"{pt['full_well_e']:.0f} | {pt['linearity_error_pct']:.2f} | "
                    f"{ch.get('lens') or '-'} |"
                )
        rows = self.results["noise_gain"].data.get("table", [])
        if rows:
            lines += [
                "",
                "| gain dB | exposure µs | signal DN | σ DN | SNR |",
                "|---|---|---|---|---|",
            ]
            lines += [
                f"| {r['gain_db']} | {r['exposure_us']} | {r['signal_dn']} | "
                f"{r['sigma_dn']} | {r['snr']} |"
                for r in rows
            ]
        (folder / "report.md").write_text("\n".join(lines) + "\n")
        restored = ""
        if self.backup_id is not None:
            rep, _ = self.service.restore_backup(self.camera_id, self.backup_id, self.actor)
            restored = (
                " ; camera restored"
                if rep.ok
                else f" ; restore reported {len(rep.failed)} problem(s)"
            )
        return self._record(
            FpResult(
                step="report",
                status="pass",
                message=f"fingerprint saved in {folder}{restored}",
                data={"folder": str(folder)},
            )
        )
