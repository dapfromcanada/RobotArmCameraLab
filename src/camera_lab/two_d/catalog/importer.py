"""Loads what camera_lab knew before the catalog (Derek 2026-10-25): the bench file's lenses,
the camera profiles and model sheets, and every fingerprint folder. Re-running it is safe:
rows are updated in place (each change logged) and a run's results are replaced.

:func:`results_from_fingerprint` is also what the fingerprint uses to write a new run."""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from camera_lab.core.paths import app_config_dir
from camera_lab.two_d.camera_models import paper_paths
from camera_lab.two_d.catalog.store import Catalog, Result
from camera_lab.two_d.fingerprint.bench import load_bench
from camera_lab.two_d.fingerprint.common import fingerprints_root

_F_RE = re.compile(r"f\s*/\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_MODEL_IN_LABEL = re.compile(r"\b((?:TRI|PHX|ATX|AP|GO|SP)[A-Z0-9]*-[A-Z0-9-]+)\b")

#: What the bench file does not say about its lenses (from the lens rings and Derek).
LENS_FACTS: dict[str, dict[str, Any]] = {
    "kowa_8mm": {
        "focal_mm": 8.0,
        "mount": "C",
        "dots": (5.6, 11.0),  # printed: 1.4 2 2.8 4 8 16; 5.6 and 11 are dots between marks
        "notes": "Kowa 8 mm C-mount. Transmission measured on the TRI054S-C, floods + acrylic, "
        "2026-10-23 (docs/experiments/20261023_iris_sweep_tri054s.txt): it passes more light "
        "stopped down than (1.4 / f)² says.",
    },
    "jai_6mm_0628": {
        "focal_mm": 6.0,
        "mount": "C",
        "dots": (),
        "notes": "JAI JVS-C118-0628-C3, 6 mm f/2.8, made for the AP-3200T's prism. Ring "
        "markings not yet checked (whole stops from f/2.8 assumed).",
    },
}

#: Names older runs recorded for a lens that has since been named by its model.
LENS_ALIASES: dict[str, str] = {"kowa 8 mm": "kowa_8mm"}  # the Kowa LM8HC (2026-10-26)

#: Runs that need a word of history (lens not stated, iris not on a mark, superseded).
RUN_FACTS: dict[str, dict[str, Any]] = {
    "233100505/20261021T202300Z": {
        "valid": False,
        "note": "before the strict procedure and the raw_hdr mode: 12-bit Bayer, no lens "
        "record, gain not pinned (a stray 28.6 dB digital gain was found 2026-10-23)",
    },
    "233100505/20261021T203734Z": {
        "valid": False,
        "note": "before the strict procedure and the raw_hdr mode (see 20261021T202300Z)",
    },
    "U330439/20261024T131257Z": {
        "lens_id": "jai_6mm_0628",
        "f_number": None,
        "note": "lens not recorded by the run (box empty): JAI 6 mm 0628-C3 at about f/4 per "
        "Derek; per-stop numbers not used",
    },
    "U330439/20261024T161546Z": {
        "f_number": None,
        "note": "recorded f/4, but the iris sat between f/4 and f/5.6 (light check 2026-10-24): "
        "camera numbers valid, per-stop numbers only from the f/8 lens row",
    },
}

#: The JAI's live checks of 2026-10-24 (camera_lab session, not a fingerprint folder).
JAI_LIVE = {
    "id": "U330439/20261024T170000Z-live",
    "started_utc": "2026-10-24T17:00:00+00:00",
    "lens_id": "jai_6mm_0628",
    "lens_text": "JAI 6 mm 0628-C3, f/2.8 (fully open)",
    "note": "live checks: short-exposure frame variation under the floods, the 12-bit frame "
    "margin for long exposures, individual gain mode at 0 dB",
    "results": [
        *(
            Result("frame_variation_pct", v, "%", exposure_us=e, note="floods, diffuser")
            for e, v in (
                (50, 6.52),
                (100, 3.75),
                (250, 2.17),
                (500, 1.02),
                (1000, 0.79),
                (2000, 0.68),
                (5000, 0.57),
            )
        ),
        Result("exposure_frame_margin_us", 1107.0, "us", pixel_format="RGB12V1Packed"),
        Result("exposure_frame_margin_us", 737.0, "us", pixel_format="RGB8"),
        Result(
            "gain_mode_signal_ratio",
            0.50,
            None,
            gain_mode="individual",
            gain_db=0.0,
            note="vs master at 0 dB, same exposure",
        ),
    ],
}

_UNITS = {
    "black_dn": "DN",
    "read_noise_dn": "DN",
    "dsnu_dn": "DN",
    "row_noise_dn": "DN",
    "dark_current_dn_s": "DN/s",
    "hot_pixel_count": "pixels",
    "system_gain_dn_per_e": "DN/e-",
    "read_noise_e": "e-",
    "full_well_e": "e-",
    "linearity_error_pct": "%",
    "prnu_pct": "%",
    "vignetting_corner_ratio": None,
    "saturation_dn": "DN",
    "snr": None,
    "sigma_dn": "DN",
    "gain_dn_per_e": "DN/e-",
    "fps_full_frame": "fps",
    "stale_frames": "frames",
    "frame_interval_ms": "ms",
    "process_ms": "ms",
    "relative_to_green": None,
    "corner_ratio": None,
    "corner_colour_cast_pct": "%",
    "green_imbalance_pct": "%",
    "temperature_c": "C",
    "light_vs_reference": None,
    "user_set_kept": None,
    "clips_at_dn": "DN",
    "signal_ratio": None,
}


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and math.isfinite(float(v)) else None


def _r(q: str, v: Any, **kw: Any) -> Result | None:
    val = _num(v)
    return Result(q, val, _UNITS.get(q), **kw) if val is not None else None


def _spread(doc: dict[str, Any], key: str) -> float | None:
    rep = (doc.get("repeats") or {}).get(key)
    return _num(rep.get("spread_pct")) if isinstance(rep, dict) else None


def f_number_of(text: str | None) -> float | None:
    m = _F_RE.search(text or "")
    return float(m.group(1)) if m else None


def results_from_fingerprint(doc: dict[str, Any], f_number: float | None = None) -> list[Result]:
    """Every measured value of a fingerprint (``fingerprint.yaml``) with its conditions."""
    out: list[Result | None] = []
    add = out.append
    t = doc.get("timing") or {}
    for fmt, fps in (t.get("fps_full_frame") or {}).items():
        add(_r("fps_full_frame", fps, pixel_format=fmt))
    add(_r("stale_frames", t.get("stale_frames_after_change")))
    pipe = t.get("pipeline") or {}
    for q in ("frame_interval_ms", "process_ms"):
        add(_r(q, pipe.get(q), pixel_format=pipe.get("pixel_format"), note=pipe.get("pc")))

    d = doc.get("dark") or {}
    for q in ("black_dn", "read_noise_dn"):
        add(_r(q, d.get(q), spread_pct=_spread(d, q), note="design gain, shortest exposure"))
    for q in ("dark_current_dn_s", "hot_pixel_count", "row_noise_dn"):
        add(_r(q, d.get(q)))
    for p in d.get("points") or []:
        for q in ("black_dn", "read_noise_dn", "dsnu_dn"):
            add(_r(q, p.get(q), gain_db=p.get("gain_db"), exposure_us=p.get("exposure_us")))
    for ch, c in (d.get("channels") or {}).items():
        for q in ("black_dn", "read_noise_dn", "dsnu_dn", "row_noise_dn"):
            add(_r(q, c.get(q), channel=ch, exposure_us=c.get("exposure_us")))

    fl = doc.get("flat") or {}
    ptc = fl.get("photon_transfer") or {}
    for q in ("system_gain_dn_per_e", "read_noise_e", "full_well_e"):
        add(_r(q, ptc.get(q), spread_pct=_spread(fl, f"photon_transfer.{q}")))
    resp = fl.get("response") or {}
    add(_r("linearity_error_pct", resp.get("linearity_error_pct", ptc.get("linearity_error_pct"))))
    add(_r("saturation_dn", resp.get("saturation_dn", ptc.get("saturation_dn"))))
    for sig, snr in resp.get("snr") or []:
        add(_r("snr", snr, signal_dn=_num(sig), note="flat sweep"))
    ff = fl.get("flat") or {}
    add(_r("prnu_pct", ff.get("prnu_pct"), spread_pct=_spread(fl, "flat.prnu_pct")))
    add(_r("vignetting_corner_ratio", ff.get("vignetting_corner_ratio"), f_number=f_number))

    col = doc.get("colour") or {}
    for p in col.get("planes") or []:
        name = p.get("name")
        for q in ("relative_to_green", "snr", "prnu_pct", "corner_ratio"):
            add(_r(q, p.get(q), plane=name, f_number=f_number if q == "corner_ratio" else None))
    add(_r("green_imbalance_pct", col.get("green_imbalance_pct")))
    add(_r("corner_colour_cast_pct", col.get("corner_colour_cast_pct"), f_number=f_number))

    ng = doc.get("noise_gain") or {}
    tables = ng.get("tables") or {"master": ng.get("table") or []}
    low = doc.get("low_light") or {}
    gain_rows = [r for t in tables.values() for r in t] + list(low.get("deferred_gain_rows") or [])
    for row in gain_rows:
        g, mode = row.get("gain_db"), row.get("gain_mode") or "master"
        for q in ("snr", "sigma_dn", "gain_dn_per_e", "read_noise_e", "full_well_e"):
            add(
                _r(
                    q,
                    row.get(q),
                    gain_db=g,
                    gain_mode=mode,
                    signal_dn=_num(row.get("signal_dn")),
                    note="noise vs gain" + (", low-light row" if row.get("row") else ""),
                )
            )
    modes = ng.get("gain_modes") or {}
    for mode, row in modes.items():
        if not isinstance(row, dict):
            continue
        for q in ("clips_at_dn", "full_well_e"):
            add(_r(q, row.get(q), gain_db=row.get("gain_db"), gain_mode=mode, note="gain modes"))
    if modes:
        add(
            _r(
                "signal_ratio",
                modes.get("signal_ratio"),
                gain_mode="individual",
                gain_db=0.0,
                note="individual vs master at the lowest gain",
            )
        )
    for p in (doc.get("dark") or {}).get("gain_points") or []:
        add(
            _r(
                "read_noise_dn",
                p.get("read_noise_dn"),
                gain_db=p.get("gain_db"),
                gain_mode=p.get("gain_mode"),
                exposure_us=p.get("exposure_us"),
                note="dark per gain",
            )
        )
    for h in (doc.get("dark") or {}).get("hot_pixels_by_exposure") or []:
        add(_r("hot_pixel_count", h.get("hot_pixels"), exposure_us=h.get("exposure_us")))
    if low:
        for lv in low.get("levels") or []:
            for q in ("snr", "sigma_dn"):
                add(
                    _r(
                        q,
                        lv.get(q),
                        signal_dn=_num(lv.get("signal_dn")),
                        exposure_us=lv.get("exposure_us"),
                        note="low light",
                    )
                )
            add(
                _r(
                    "model_snr",
                    lv.get("model_snr"),
                    signal_dn=_num(lv.get("signal_dn")),
                    note="low light",
                )
            )
        for row in (low.get("gain_fixed_exposure") or {}).get("rows") or []:
            add(
                _r(
                    "snr",
                    row.get("snr"),
                    gain_db=row.get("gain_db"),
                    gain_mode=row.get("gain_mode"),
                    exposure_us=row.get("exposure_us"),
                    note="low light, fixed exposure",
                )
            )
        for a in low.get("averaging") or []:
            add(
                _r(
                    "averaging_snr_gain",
                    a.get("snr_gain"),
                    setting=f"{a.get('frames')} frames",
                    note="low light",
                )
            )

    for ch, c in (doc.get("hdr_channels") or {}).items():
        pt = c.get("photon_transfer") or {}
        for q in ("system_gain_dn_per_e", "full_well_e", "linearity_error_pct"):
            add(_r(q, pt.get(q), channel=ch, f_number=f_number_of(c.get("lens"))))

    stop_rows = [v for k, v in doc.items() if k.startswith("lens_iris") and isinstance(v, dict)]
    if low.get("f_number") is not None:
        stop_rows.append(low)
    for li in stop_rows:
        lf = _num(li.get("f_number")) or f_number_of(li.get("lens"))
        add(
            _r(
                "light_vs_reference",
                li.get("light_vs_reference", li.get("light_vs_diffuser_row")),
                f_number=lf,
            )
        )
        add(_r("vignetting_corner_ratio", li.get("vignetting_corner_ratio"), f_number=lf))
        for plane, v in (li.get("corner_ratio") or {}).items():
            add(_r("corner_ratio", v, plane=plane, f_number=lf))
        add(_r("corner_colour_cast_pct", li.get("corner_colour_cast_pct"), f_number=lf))

    pers = (doc.get("user_sets") or {}).get("persistence") or {}
    for name in pers.get("kept") or []:
        add(
            Result(
                "user_set_kept",
                1.0,
                None,
                setting=name,
                note=f"software reboot, {pers.get('slot')}",
            )
        )
    for name in pers.get("lost") or []:
        add(
            Result(
                "user_set_kept",
                0.0,
                None,
                setting=name,
                note=f"software reboot, {pers.get('slot')}",
            )
        )

    for setup, temp in (doc.get("temperatures_c") or {}).items():
        add(_r("temperature_c", temp, note=f"start of the {setup} row"))
    return [r for r in out if r is not None]


@dataclass(frozen=True)
class ImportReport:
    camera_models: int
    cameras: int
    lenses: int
    runs: int
    results: int
    pair_stops: int
    notes: tuple[str, ...]


#: Vendor by the start of a profile label (the sheet's vendor wins where there is a sheet).
_VENDORS = {"Lucid": "Lucid Vision Labs", "JAI": "JAI Corporation"}


def _profiles(root: Path) -> Iterator[tuple[dict[str, Any], str]]:
    """(profile, its text): the header comment names the firmware where the label does not."""
    for p in sorted(root.glob("*.yaml")):
        text = p.read_text()
        doc = yaml.safe_load(text)
        if isinstance(doc, dict) and doc.get("serial") and not str(doc["serial"]).startswith("SIM"):
            yield doc, text


def import_all(
    cat: Catalog,
    *,
    config_dir: Path | None = None,
    fingerprints: Path | None = None,
) -> ImportReport:
    cfg = config_dir or app_config_dir()
    fp_root = fingerprints or fingerprints_root()
    notes: list[str] = []

    # lenses (the bench file's list, plus what its rings say)
    bench = load_bench(cfg / "fingerprint_bench.yaml")
    n_lenses = 0
    for lid, lens in bench.lenses.items() if bench is not None else ():
        facts = LENS_FACTS.get(lid, {})
        dots = tuple(facts.get("dots", ()))
        cat.add_lens(
            lens.label,
            lens_id=lid,
            focal_mm=facts.get("focal_mm"),
            mount=facts.get("mount"),
            stops=[f for f in lens.f_stops if f not in dots],
            dots=dots,
            notes=facts.get("notes"),
            actor="import",
            reason="from config/fingerprint_bench.yaml",
        )
        cat.set_transmission(
            lid,
            lens.open_f,
            1.0,
            measured_utc="definition",
            actor="import",
            reason="fully open is the reference",
        )
        for f, tr in lens.transmission.items():
            cat.set_transmission(
                lid,
                f,
                tr,
                measured_utc="2026-10-23",
                actor="import",
                reason="bench file transmission table",
            )
        n_lenses += 1

    # camera models and cameras (profiles, model sheets, the latest fingerprint's identity)
    idents: dict[str, dict[str, Any]] = {}
    for fy in sorted(fp_root.glob("*/*/fingerprint.yaml")):
        ident = (yaml.safe_load(fy.read_text()) or {}).get("identity") or {}
        if ident.get("serial"):
            idents[str(ident["serial"])] = ident
    models: set[str] = set()
    n_cameras = 0
    for prof, prof_text in _profiles(cfg / "camera_profiles"):
        serial = str(prof["serial"])
        ident = idents.get(serial, {})
        m = _MODEL_IN_LABEL.search(str(prof.get("label", "")))
        model = ident.get("model") or (m.group(1) if m else None)
        if model is None:
            notes.append(f"{serial}: no model found in the profile label or a fingerprint")
            continue
        ch = prof.get("characteristics") or {}
        paths = paper_paths(model, cfg / "camera_models")
        sheet = yaml.safe_load(paths[1].read_text()) if paths is not None else {}
        cat.upsert_camera_model(
            model,
            vendor=sheet.get("vendor")
            or ident.get("vendor")
            or next(
                (v for k, v in _VENDORS.items() if str(prof.get("label", "")).startswith(k)), None
            ),
            sensor=(sheet.get("sensor") or {}).get("name"),
            shutter=ch.get("shutter", "unknown"),
            colour=ch.get("colour", "unknown"),
            hdr=bool(ch.get("hdr_capable")),
            design_mode=prof.get("default_mode"),
            actor="import",
            reason="from the camera profile and model sheet",
        )
        models.add(model)
        fw = ident.get("firmware")
        if fw is None:
            fm = re.search(r"\bfw ([0-9][0-9.]*[0-9])", prof_text)
            fw = fm.group(1) if fm else None
        cat.upsert_camera(
            serial,
            model,
            firmware=fw,
            location="bench",
            actor="import",
            reason="from config/camera_profiles",
        )
        n_cameras += 1

    # fingerprint runs and their results
    n_runs = n_results = n_pair = 0
    known = {c["serial"] for c in cat.cameras()}
    for fy in sorted(fp_root.glob("*/*/fingerprint.yaml")):
        serial, stamp = fy.parent.parent.name, fy.parent.name
        if serial not in known:
            notes.append(f"{serial}/{stamp}: camera not in the catalog, skipped")
            continue
        res, pairs = import_run(cat, fy, actor="import")
        n_results += res
        n_pair += pairs
        n_runs += 1

    # live checks
    if "U330439" in known:
        live = JAI_LIVE
        n_results += cat.add_run(
            str(live["id"]),
            "U330439",
            kind="live_check",
            started_utc=str(live["started_utc"]),
            status="pass",
            results=list(live["results"]),  # type: ignore[arg-type]
            lens_id=str(live["lens_id"]),
            lens_text=str(live["lens_text"]),
            f_number=2.8,
            note=str(live["note"]),
            actor="import",
        )
        n_runs += 1
    return ImportReport(len(models), n_cameras, n_lenses, n_runs, n_results, n_pair, tuple(notes))


def import_run(
    cat: Catalog,
    fingerprint_yaml: Path,
    *,
    lens_id: str | None = None,
    actor: Literal["fingerprint", "import"] = "fingerprint",
) -> tuple[int, int]:
    """One fingerprint folder into the catalog: the run, its results, the pair's per-stop
    shading and the lens's measured light per stop. Returns (results, per-stop values). The
    fingerprint calls it when it writes its report; the camera must be in the catalog."""
    serial, stamp = fingerprint_yaml.parent.parent.name, fingerprint_yaml.parent.name
    doc = yaml.safe_load(fingerprint_yaml.read_text()) or {}
    run_id = f"{serial}/{stamp}"
    facts = RUN_FACTS.get(run_id, {})
    ident = doc.get("identity") or {}
    lens_text = ident.get("lens") or None
    if lens_id is None:
        found = cat.find_lens(lens_text) if lens_text else None
        alias = next(
            (
                lid
                for name, lid in LENS_ALIASES.items()
                if (lens_text or "").lower().startswith(name)
            ),
            None,
        )
        lens_id = facts.get("lens_id", found.id if found else alias)
    f_ref = facts["f_number"] if "f_number" in facts else f_number_of(lens_text)
    temps = [float(v) for v in (doc.get("temperatures_c") or {}).values()]
    results = results_from_fingerprint(doc, f_ref)
    n = cat.add_run(
        run_id,
        serial,
        kind="fingerprint",
        started_utc=str(doc.get("created_utc")),
        status="pass",  # a fingerprint folder is written only when the finish row passes
        results=results,
        lens_id=lens_id,
        lens_text=lens_text,
        f_number=f_ref,
        valid=bool(facts.get("valid", True)),
        note=facts.get("note"),
        folder=str(fingerprint_yaml.parent),
        temperature_min_c=min(temps) if temps else None,
        temperature_max_c=max(temps) if temps else None,
        actor=actor,
    )
    model = cat.model_of(serial)
    if not facts.get("valid", True) or lens_id is None or model is None:
        return n, 0
    pairs = _pair_numbers(
        cat, model, lens_id, run_id, f_ref, results, str(doc.get("created_utc")), actor
    )
    return n, pairs


def _pair_numbers(
    cat: Catalog,
    model: str,
    lens_id: str,
    run_id: str,
    f_ref: float | None,
    results: list[Result],
    started_utc: str,
    actor: Literal["fingerprint", "import"] = "import",
) -> int:
    """Per-stop pair numbers (corner ratios per plane) and the lens's measured light."""
    lens = cat.lens(lens_id)
    marks = {s.f_number for s in lens.stops} if lens is not None else set()
    n = 0
    for r in results:
        if r.f_number is None or r.f_number not in marks or r.value is None:
            continue
        if r.quantity == "corner_ratio" and r.plane:
            cat.set_pair_stop(
                model, lens_id, r.f_number, r.plane, r.value, fingerprint_id=run_id, actor=actor
            )
            n += 1
        elif r.quantity == "vignetting_corner_ratio" and not any(
            x.quantity == "corner_ratio" and x.f_number == r.f_number for x in results
        ):
            cat.set_pair_stop(
                model, lens_id, r.f_number, "all", r.value, fingerprint_id=run_id, actor=actor
            )
            n += 1
        elif (
            r.quantity == "light_vs_reference"
            and lens is not None
            and f_ref is not None
            and f_ref == lens.open_f
        ):
            cat.set_transmission(
                lens_id,
                r.f_number,
                r.value,
                fingerprint_id=run_id,
                measured_utc=started_utc,
                actor=actor,
                reason="lens row against fully open",
            )
    return n
