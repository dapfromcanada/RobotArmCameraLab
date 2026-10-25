"""White papers generated from the catalog (Derek 2026-10-25: "files are messy"): the
camera paper ``config/camera_models/<model>/camera.md`` + ``camera.yaml`` and, per lens
tested on the model, the pair paper ``config/camera_models/<model>/lenses/<lens>.md`` +
``.yaml``. Nothing in them is edited by hand: the prose is ``paper_sections`` rows, the
sheet is ``model_facts`` cells, the numbers are ``results`` / ``lens_stops`` /
``pair_stops``. Change a row or a cell, then regenerate (``catalog papers``).

:func:`import_papers` loads the hand-written papers once (sections split at ``## ``
headings, the sheet flattened into cells); :func:`update_facts_from_run` puts a new
fingerprint's numbers into the cells the tools read."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from camera_lab.core.paths import app_config_dir, safe_join
from camera_lab.two_d.catalog.store import Catalog, Result

SEP = "/"
CAMERA_PAPER = "camera"
_TITLE_RE = re.compile(r"^# (.+?)( — camera white paper.*)?$")


# ---------------------------------------------------------------- facts
def flatten(doc: Any, prefix: str = "") -> dict[str, Any]:
    """A sheet as cells: dicts are walked, everything else (numbers, text, lists) is a leaf."""
    if isinstance(doc, dict) and doc:
        out: dict[str, Any] = {}
        for k, v in doc.items():
            out.update(flatten(v, f"{prefix}{SEP}{k}" if prefix else str(k)))
        return out
    return {prefix: doc} if prefix else {}


def unflatten(cells: dict[str, Any]) -> dict[str, Any]:
    """Cells back into the sheet, in the cells' order (the order they were first written)."""
    out: dict[str, Any] = {}
    for key, value in cells.items():
        node = out
        parts = key.split(SEP)
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    return out


# ---------------------------------------------------------------- import
def split_sections(md: str) -> list[tuple[str, str]]:
    """(heading, body) per ``## `` section; the text before the first one has heading ''."""
    out: list[tuple[str, str]] = []
    heading, body = "", list[str]()
    for line in md.splitlines():
        if line.startswith("## "):
            out.append((heading, "\n".join(body).strip("\n")))
            heading, body = line[3:].strip(), []
        else:
            body.append(line)
    out.append((heading, "\n".join(body).strip("\n")))
    return [(h, b) for h, b in out if h or b]


def import_papers(cat: Catalog, config_dir: Path | None = None) -> dict[str, int]:
    """The hand-written ``config/camera_models/<model>.md`` / ``.yaml`` into the catalog.
    Returns {model: number of facts}."""
    root = (config_dir or app_config_dir()) / "camera_models"
    known = {m["id"] for m in cat.camera_models()}
    out: dict[str, int] = {}
    for md in sorted(root.glob("*.md")):
        model = md.stem
        if model not in known:
            continue
        sections = [
            (h, b)
            for h, b in split_sections(md.read_text())
            if "(generated from the catalog)" not in h  # a generated paper read back
        ]
        for i, (heading, body) in enumerate(sections):
            cat.set_section(model, i, heading, body, actor="import", reason=f"from {md.name}")
        sheet = root / f"{model}.yaml"
        if sheet.is_file():
            doc = yaml.safe_load(sheet.read_text()) or {}
            cells = flatten(doc)
            for key, value in cells.items():
                cat.set_fact(
                    model, key, value, source="import", actor="import", reason=f"from {sheet.name}"
                )
            out[model] = len(cells)
    return out


# --------------------------------------------- facts from a new fingerprint
def update_facts_from_run(cat: Catalog, model: str, run_id: str) -> list[str]:
    """The cells the site tools read, from a fingerprint's own numbers (each change logged
    with the run as its source). Returns the keys that changed."""
    res = cat.results(run_id)

    def one(q: str, **cond: Any) -> float | None:
        for r in res:
            if (
                r.quantity == q
                and r.value is not None
                and all(getattr(r, k) == v for k, v in cond.items())
            ):
                return r.value
        return None

    changed: list[str] = []

    def put(key: str, value: Any) -> None:
        if isinstance(value, dict):  # a whole table replaces its cells
            for old in [k for k in cat.facts(model) if k.startswith(key + SEP)]:
                cat.delete_fact(model, old, actor="fingerprint")
        if value is not None and cat.set_fact(
            model, key, value, source=run_id, actor="fingerprint", reason="fingerprint result"
        ):
            changed.append(key)

    black = one("black_dn", note="design gain, shortest exposure")
    rn = one("read_noise_dn", note="design gain, shortest exposure")
    put("performance/black_dn", _round(black))
    put("performance/read_noise_dn", _round(rn))
    put("performance/row_noise_dn", _round(one("row_noise_dn")))
    sat = one("saturation_dn")
    if sat and black is not None and rn:
        put("performance/dynamic_range_stops", round(math.log2((sat - black) / rn), 2))
        put("performance/dynamic_range_db", round(20 * math.log10((sat - black) / rn), 1))
    for r in res:
        if r.quantity == "fps_full_frame" and r.pixel_format and r.value:
            put(f"timing/fps_full_frame/{r.pixel_format}", round(r.value, 2))
    for q, key in (
        ("system_gain_dn_per_e", "performance/system_gain_dn_per_e"),
        ("full_well_e", "performance/full_well_e"),
        ("linearity_error_pct", "performance/linearity_error_pct"),
        ("prnu_pct", "performance/prnu_pct"),
    ):
        put(key, _round(one(q, gain_db=None, gain_mode=None, channel=None)))
    full = cat.facts(model).get("performance/full_scale_dn")
    snr = [r for r in res if r.quantity == "snr" and r.note == "flat sweep" and r.signal_dn]
    if snr and isinstance(full, int | float) and full > 0:
        put(
            "performance/snr_vs_signal/points",
            {round(r.signal_dn / full, 4): round(r.value or 0, 1) for r in snr},  # type: ignore[operator]
        )
    return changed


def _round(v: float | None, digits: int = 4) -> float | None:
    return None if v is None else float(f"{v:.{digits}g}")


# --------------------------------------------------------------- render
def _table(head: Iterable[str], rows: Iterable[Iterable[Any]]) -> list[str]:
    h = list(head)
    out = ["| " + " | ".join(h) + " |", "|" + "---|" * len(h)]
    out += ["| " + " | ".join(_fmt(c) for c in r) + " |" for r in rows]
    return out


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def _runs(cat: Catalog, model: str) -> list[dict[str, Any]]:
    serials = {c["serial"] for c in cat.cameras() if c["model_id"] == model}
    return [r for r in cat.runs() if r["serial"] in serials]


def _measured(cat: Catalog, model: str) -> list[str]:
    """The generated section: every valid run's headline numbers side by side, then the
    latest run's complete tables (darks per exposure, gain, low light, user sets)."""
    runs = _runs(cat, model)
    fps = [r for r in runs if r["kind"] == "fingerprint" and r["valid"]]
    out = ["Every number below is from the catalog (`camera-lab two-d catalog runs`)."]
    if runs:
        out += [
            "",
            *_table(
                ("run", "kind", "lens", "iris", "°C", "used"),
                (
                    (
                        r["id"],
                        r["kind"],
                        r["lens_id"],
                        f"f/{r['f_number']:g}" if r["f_number"] else None,
                        (
                            f"{r['temperature_min_c']:.1f}–{r['temperature_max_c']:.1f}"
                            if r["temperature_min_c"] is not None
                            else None
                        ),
                        "yes" if r["valid"] else "history only",
                    )
                    for r in runs
                ),
            ),
        ]
    heads: tuple[tuple[str, str, dict[str, Any]], ...] = (
        ("black_dn", "black DN", {"note": "design gain, shortest exposure"}),
        ("read_noise_dn", "read noise DN", {"note": "design gain, shortest exposure"}),
        ("row_noise_dn", "row noise DN", {}),
        ("dark_current_dn_s", "dark current DN/s", {}),
        ("system_gain_dn_per_e", "DN/e-", {"gain_db": None}),
        ("full_well_e", "full well e-", {"gain_db": None}),
        ("linearity_error_pct", "linearity %", {}),
        ("prnu_pct", "PRNU %", {"plane": None}),
    )
    if fps:
        rows = []
        for r in fps:
            res = cat.results(r["id"])
            row: list[Any] = [r["id"].split("/")[-1]]
            for q, _, cond in heads:
                v = next(
                    (
                        x.value
                        for x in res
                        if x.quantity == q and all(getattr(x, k) == val for k, val in cond.items())
                    ),
                    None,
                )
                row.append(v)
            rows.append(row)
        out += ["", "### Headline numbers per fingerprint", ""]
        out += _table(("run", *(h for _, h, _ in heads)), rows)
        last = cat.results(fps[-1]["id"])
        out += _latest_tables(last, fps[-1]["id"])
    pairs = cat.pairs(model)
    if pairs:
        out += ["", "### Lenses tested on this camera", ""]
        out += [f"- `{p}`: see `lenses/{p}.md`" for p in pairs]
    return out


def _latest_tables(res: list[Result], run_id: str) -> list[str]:
    out: list[str] = []
    darks = sorted(
        (
            x
            for x in res
            if x.quantity == "read_noise_dn"
            and x.exposure_us
            and x.gain_db is not None
            and x.note is None
        ),
        key=lambda x: x.exposure_us or 0,
    )
    if darks:
        black: dict[float | None, float | None] = {
            x.exposure_us: x.value for x in res if x.quantity == "black_dn" and x.exposure_us
        }
        hot: dict[float | None, float | None] = {
            x.exposure_us: x.value for x in res if x.quantity == "hot_pixel_count" and x.exposure_us
        }
        g0 = min(x.gain_db for x in darks if x.gain_db is not None)
        out += ["", f"### Darks per exposure ({run_id})", ""]
        out += _table(
            ("exposure ms", "black DN", "read noise DN", "hot pixels"),
            (
                (
                    (x.exposure_us or 0) / 1000,
                    black.get(x.exposure_us),
                    x.value,
                    hot.get(x.exposure_us),
                )
                for x in darks
                if x.gain_db == g0
            ),
        )
    gain = [x for x in res if x.quantity == "snr" and x.note and x.note.startswith("noise vs gain")]
    if gain:
        by = {(x.gain_mode, x.gain_db): x for x in gain}

        def q(name: str, mode: str | None, g: float | None) -> float | None:
            return next(
                (
                    x.value
                    for x in res
                    if x.quantity == name
                    and x.gain_mode == mode
                    and x.gain_db == g
                    and x.note
                    and x.note.startswith("noise vs gain")
                ),
                None,
            )

        out += ["", f"### Noise vs gain ({run_id})", ""]
        out += _table(
            ("gain mode", "gain dB", "SNR", "DN/e-", "read noise e-", "full well e-"),
            (
                (
                    m,
                    g,
                    x.value,
                    q("gain_dn_per_e", m, g),
                    q("read_noise_e", m, g),
                    q("full_well_e", m, g),
                )
                for (m, g), x in sorted(by.items(), key=lambda kv: (str(kv[0][0]), kv[0][1] or 0))
            ),
        )
    low = [x for x in res if x.quantity == "snr" and x.note == "low light"]
    if low:
        model = {x.signal_dn: x.value for x in res if x.quantity == "model_snr"}
        out += ["", f"### Low light ({run_id})", ""]
        out += _table(
            ("signal DN", "SNR", "model SNR"),
            (
                (x.signal_dn, x.value, model.get(x.signal_dn))
                for x in sorted(low, key=lambda x: x.signal_dn or 0)
            ),
        )
    kept = [x for x in res if x.quantity == "user_set_kept"]
    if kept:
        out += ["", f"### What a user set keeps ({run_id})", ""]
        out += _table(
            ("setting", "after a restart", "how"),
            ((x.setting, "kept" if x.value else "LOST", x.note) for x in kept),
        )
    return out


def camera_paper(cat: Catalog, model: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    sections = cat.sections(model)
    runs = [r["id"] for r in _runs(cat, model) if r["valid"]]
    lines: list[str] = []
    numbered = sum(1 for h, _ in sections if re.match(r"\d+\.", h))
    for heading, body in sections:
        if heading == "":
            first, _, rest = body.partition("\n")
            m = _TITLE_RE.match(first)
            title = m.group(1) if m else model
            lines += [
                f"# {title} — camera white paper (generated {stamp} from the camera catalog; "
                f"runs: {', '.join(runs) or 'none'})",
                rest,
            ]
        else:
            lines += ["", f"## {heading}", body]
    lines += ["", f"## {numbered + 1}. Measured data (generated from the catalog)"]
    lines += _measured(cat, model)
    lines += [
        "",
        "---",
        "Generated from `WS/config/camera_catalog.sqlite`: edit the catalog (`camera-lab two-d "
        "catalog ...`), never this file.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def pair_paper(cat: Catalog, model: str, lens_id: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    lens = cat.lens(lens_id)
    name = lens.name if lens else lens_id
    lines = [
        f"# {name} on the {model} — lens white paper (generated {stamp} from the camera catalog)",
        "",
        f"What this lens does on this camera model (vignetting and colour shading depend on the "
        f"sensor size and on a prism or Bayer sensor). Lens: {name}"
        + (f", {lens.focal_mm:g} mm" if lens and lens.focal_mm else "")
        + (f", {lens.mount}-mount" if lens and lens.mount else "")
        + (f". {lens.notes}" if lens and lens.notes else "."),
    ]
    for heading, body in cat.sections(model, lens_id):
        lines += ["", f"## {heading}" if heading else "", body]
    stops = {s.f_number: s for s in (lens.stops if lens else ())}
    ps = cat.pair_stops(model, lens_id)
    planes = sorted({p["plane"] for p in ps})
    corner = {(p["f_number"], p["plane"]): p["corner_ratio"] for p in ps}
    fs = sorted(set(stops) | {p["f_number"] for p in ps})
    lines += [
        "",
        "## Every marked stop",
        "",
        "Light relative to fully open (measured where the "
        "catalog has it), corners relative to the centre per colour plane:",
        "",
    ]
    lines += _table(
        ("stop", "end stop", "light", *(f"corner {p}" for p in planes), "colour cast %"),
        (
            (
                f"f/{f:g}",
                stops[f].end_stop if f in stops else None,
                stops[f].transmission if f in stops else None,
                *(corner.get((f, p)) for p in planes),
                _cast([corner.get((f, p)) for p in planes if p != "all"]),
            )
            for f in fs
        ),
    )
    lines += ["", "---", "Generated from `WS/config/camera_catalog.sqlite`; never edit this file."]
    return "\n".join(lines).rstrip() + "\n"


def _cast(ratios: list[float | None]) -> float | None:
    vals = [r for r in ratios if r]
    return round((max(vals) / min(vals) - 1) * 100, 2) if len(vals) > 1 else None


def pair_sheet(cat: Catalog, model: str, lens_id: str) -> dict[str, Any]:
    lens = cat.lens(lens_id)
    ps = cat.pair_stops(model, lens_id)
    return {
        "schema_version": 1,
        "model": model,
        "lens": lens_id,
        "lens_name": lens.name if lens else lens_id,
        "stops": [
            {
                "f_number": s.f_number,
                "end_stop": s.end_stop,
                "light_vs_open": s.transmission,
                "corner_ratio": {
                    p["plane"]: p["corner_ratio"] for p in ps if p["f_number"] == s.f_number
                },
            }
            for s in (lens.stops if lens else ())
        ],
    }


def papers_out_dir() -> Path:
    """Where generated papers go: ``CAMERA_LAB_PAPERS_OUT`` (tests) or
    ``APP/config/camera_models``."""
    override = os.environ.get("CAMERA_LAB_PAPERS_OUT")
    return Path(override) if override else app_config_dir() / "camera_models"


def write_papers(cat: Catalog, model: str, root: Path | None = None) -> list[Path]:
    """Write the model's camera paper and every pair paper; returns the files written. A model
    the catalog knows nothing measured or written about yet gets no paper."""
    facts = cat.facts(model)
    if not facts and not cat.sections(model) and not _runs(cat, model):
        return []
    base = safe_join(root or papers_out_dir(), model)
    (base / "lenses").mkdir(parents=True, exist_ok=True)
    head = "# Generated from WS/config/camera_catalog.sqlite: edit the catalog, not this file.\n"
    written = []
    md = base / f"{CAMERA_PAPER}.md"
    md.write_text(camera_paper(cat, model))
    written.append(md)
    if facts:  # the sheet the site tools read (only once it has cells)
        y = base / f"{CAMERA_PAPER}.yaml"
        sheet = unflatten(facts)
        y.write_text(
            head
            + yaml.safe_dump(json.loads(json.dumps(sheet)), sort_keys=False, allow_unicode=True)
        )
        written.append(y)
    for lens_id in cat.pairs(model):
        p = base / "lenses" / f"{lens_id}.md"
        p.write_text(pair_paper(cat, model, lens_id))
        q = base / "lenses" / f"{lens_id}.yaml"
        q.write_text(head + yaml.safe_dump(pair_sheet(cat, model, lens_id), sort_keys=False))
        written += [p, q]
    return written
