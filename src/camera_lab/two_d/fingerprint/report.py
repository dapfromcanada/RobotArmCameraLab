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

    def _setups_for_report(self) -> dict[str, Any]:
        """Each row's status. The row that writes the report is still running: everything
        before the report passed, so it is recorded as passing (2026-10-25: a Retry of the
        finish row left its earlier STOP in the file)."""
        out: dict[str, Any] = {
            s: r.model_dump(mode="json", exclude={"data"}) for s, r in self.setup_results.items()
        }
        for setup, plan in self.plan.items():
            if "report" in plan.measures:
                out[setup] = {
                    "step": setup,
                    "status": "pass",
                    "message": "this row wrote the report (every measurement before it passed)",
                }
        return out

    @staticmethod
    def _gain_table(rows: list[dict[str, Any]]) -> list[str]:
        out = [
            "",
            "| gain dB | mode | exposure µs | signal DN | σ DN | SNR | DN/e- | read noise e- "
            "| full well e- |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        out += [
            f"| {r['gain_db']} | {r.get('gain_mode') or '-'} | {r['exposure_us']} | "
            f"{r['signal_dn']} | {r['sigma_dn']} | {r['snr']} | {r.get('gain_dn_per_e') or '-'} | "
            f"{r.get('read_noise_e') or '-'} | {r.get('full_well_e') or '-'} |"
            + (f" ({r['row']} row)" if r.get("row") else "")
            for r in rows
        ]
        return out

    def _gain_lines(self) -> list[str]:
        """Noise vs gain at a constant output: every step, each gain mode (low-light steps
        appended), then the gain modes side by side."""
        gain = self.results.get("noise_gain")
        if gain is None or gain.status != "pass":
            return []
        tables = gain.data.get("tables") or {"": gain.data.get("table", [])}
        low = self.results.get("low_light")
        extra = low.data.get("deferred_gain_rows", []) if low is not None else []
        rows = [r for t in tables.values() for r in t] + list(extra)
        out = ["", "Noise vs gain at a constant output (every gain step):"] if rows else []
        out += self._gain_table(rows) if rows else []
        modes = gain.data.get("gain_modes")
        if modes:
            out.append(f"\nGain modes: {modes['summary']}.")
        return out

    def _dark_lines(self) -> list[str]:
        """Darks at every exposure (design gain) and the hot pixels per exposure."""
        dark = self.results.get("dark")
        if dark is None or dark.status != "pass":
            return []
        pts = [
            p
            for p in dark.data.get("points", [])
            if p["gain_db"] == min(q["gain_db"] for q in dark.data.get("points", []))
        ]
        hot = {
            h["exposure_us"]: h["hot_pixels"] for h in dark.data.get("hot_pixels_by_exposure", [])
        }
        if not pts:
            return []
        out = [
            "",
            "Darks (cap on) at every exposure:",
            "",
            "| exposure ms | black DN | read noise DN | DSNU DN | hot pixels |",
            "|---|---|---|---|---|",
        ]
        out += [
            f"| {p['exposure_us'] / 1000:g} | {p['black_dn']:.2f} | {p['read_noise_dn']:.3f} | "
            f"{p['dsnu_dn']:.3f} | {hot.get(p['exposure_us'], '-')} |"
            for p in sorted(pts, key=lambda p: p["exposure_us"])
        ]
        gp = dark.data.get("gain_points") or []
        if gp:
            out += [
                "",
                "| gain dB | mode | black DN | read noise DN (10 ms) |",
                "|---|---|---|---|",
            ]
            out += [
                f"| {p['gain_db']:g} | {p.get('gain_mode') or '-'} | {p['black_dn']:.2f} | "
                f"{p['read_noise_dn']:.3f} |"
                for p in gp
            ]
        return out

    def _lens_lines(self, ident: dict[str, Any]) -> list[str]:
        """Every marked stop: the light it passes, vignetting, per-colour corners."""
        colour = self.results.get("colour")
        flat = self.results.get("flat")
        rows: list[tuple[float, float | None, dict[str, Any]]] = []
        if flat is not None and flat.status == "pass":
            open_corners = colour.data.get("corner_ratio", {}) if colour is not None else {}
            rows.append(
                (
                    self._reference().f_number,
                    1.0,
                    {
                        "vignetting_corner_ratio": flat.data.get("flat", {}).get(
                            "vignetting_corner_ratio"
                        ),
                        "corner_ratio": open_corners,
                        "corner_colour_cast_pct": (
                            colour.data.get("corner_colour_cast_pct") if colour else None
                        ),
                    },
                )
            )
        for m, r in self.results.items():
            if (m.startswith("lens_iris") or m == "low_light") and r.status == "pass":
                if m == "low_light" and "f_number" not in r.data:
                    continue
                rows.append((r.data["f_number"], r.data.get("light_vs_reference"), r.data))
        if len(rows) < 2:
            return []
        planes = sorted({k for _, _, d in rows for k in (d.get("corner_ratio") or {})})
        out = [
            "",
            f"Lens {self.lens_label or ident.get('lens') or ''}: every marked stop "
            "(diffuser, floods; light relative to fully open):",
            "",
            "| stop | light | vignetting | "
            + " | ".join(f"corner {p}" for p in planes)
            + " | colour cast % |",
            "|---|---|---|" + "---|" * len(planes) + "---|",
        ]
        for f, light, d in sorted(rows, key=lambda x: x[0]):
            corners = d.get("corner_ratio") or {}
            vig = d.get("vignetting_corner_ratio")
            cast = d.get("corner_colour_cast_pct")
            out.append(f"| f/{f:g} | {light:.4f} | " if light is not None else f"| f/{f:g} | - | ")
            out[-1] += (
                (f"{vig:.3f} | " if vig is not None else "- | ")
                + "".join(f"{corners[p]:.3f} | " if p in corners else "- | " for p in planes)
                + (f"{cast:.2f} |" if cast is not None else "- |")
            )
        return out

    def _low_light_lines(self) -> list[str]:
        low = self.results.get("low_light")
        if low is None or low.status != "pass":
            return []
        d = low.data
        out = [
            "",
            f"Low light (iris fully closed, light {d['light_vs_reference']:.4f}× fully open):",
            "",
            "| exposure µs | signal DN | σ DN | SNR | model SNR | at lowest code % "
            "| distinct values |",
            "|---|---|---|---|---|---|---|",
        ]
        out += [
            f"| {r['exposure_us']} | {r['signal_dn']} | {r['sigma_dn']} | {r['snr']} | "
            f"{r['model_snr']} | {r['at_lowest_code_pct']} | {r['distinct_values']} |"
            for r in d.get("levels", [])
        ]
        fixed = d.get("gain_fixed_exposure") or {}
        if fixed.get("rows"):
            out += ["", f"Gain at a fixed exposure ({fixed['exposure_us'] / 1000:g} ms):"]
            out += self._gain_table(fixed["rows"])
        avg = d.get("averaging") or []
        if avg:
            out += ["", "| frames averaged | σ DN | SNR gain | √N |", "|---|---|---|---|"]
            out += [
                f"| {a['frames']} | {a['sigma_dn']} | {a['snr_gain']} | {a['sqrt_n']} |"
                for a in avg
            ]
        return out

    def _to_catalog(self, folder: Path) -> str:
        """The run into the catalog (the source of truth, Derek 2026-10-25): the camera model
        and camera, the lens if the catalog does not have it yet, the run with every result,
        the pair's per-stop shading and the lens's light per stop. A failure here never loses
        the fingerprint: the folder is written first, and `catalog import` can redo it."""
        from camera_lab.two_d.catalog import Catalog
        from camera_lab.two_d.catalog.importer import import_run

        ident = self.results["identity"].data
        try:
            with Catalog(self.catalog_path) as cat:
                ch = self.service.get_profile(self.camera_id).characteristics
                cat.upsert_camera_model(
                    str(ident["model"]),
                    vendor=ident.get("vendor"),
                    shutter=ch.shutter,
                    colour=ch.colour,
                    hdr=ch.hdr_capable,
                    design_mode=self.mode,
                    actor="fingerprint",
                )
                cat.upsert_camera(
                    str(ident["serial"]),
                    str(ident["model"]),
                    firmware=ident.get("firmware"),
                    actor="fingerprint",
                )
                if self.lens_id and self.lens_spec and cat.lens(self.lens_id) is None:
                    cat.add_lens(
                        self.lens_spec.label,
                        lens_id=self.lens_id,
                        focal_mm=None,
                        mount=None,
                        stops=self.lens_spec.f_stops,
                        actor="fingerprint",
                        reason="lens of a fingerprint, from the bench file",
                    )
                n, pairs = import_run(cat, folder / "fingerprint.yaml", lens_id=self.lens_id)
        except Exception as exc:  # the files are written: never lose the fingerprint
            return f" ; catalog NOT updated ({type(exc).__name__}: {exc}): run `catalog import`"
        return f" ; catalog: {n} results, {pairs} per-stop values"

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
                    "lens_iris",
                ],
            },
            "temperatures_c": dict(self.temperatures),
            **{s: self.results[s].data for s in FP_STEPS if s in self.results and s != "report"},
            "hdr_channels": dict(self._channels),
            "setups": self._setups_for_report(),
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
        ]
        if self.temperatures:
            lines.append(
                "- **Camera temperature:** "
                + ", ".join(f"{s} {t:.1f} °C" for s, t in self.temperatures.items())
            )
        lines.append("")
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
        lines += self._gain_lines()
        lines += self._dark_lines()
        lines += self._lens_lines(ident)
        lines += self._low_light_lines()
        (folder / "report.md").write_text("\n".join(lines) + "\n")
        catalog_note = self._to_catalog(folder)
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
                message=f"fingerprint saved in {folder}{restored}{catalog_note}",
                data={"folder": str(folder)},
            )
        )
