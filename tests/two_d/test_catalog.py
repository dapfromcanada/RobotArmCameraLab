"""The camera and lens catalog (SQLite, Phase 11, Derek 2026-10-25)."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

from camera_lab.core.paths import app_config_dir
from camera_lab.two_d.catalog import Catalog, CatalogError, Result
from camera_lab.two_d.catalog.importer import import_all, results_from_fingerprint
from camera_lab.two_d.catalog.schema import SCHEMA_VERSION
from camera_lab.two_d.catalog.store import lens_id_for


@pytest.fixture
def cat(tmp_path: Path) -> Catalog:
    c = Catalog(tmp_path / "catalog.sqlite")
    c.upsert_camera_model(
        "AP-3200T-PGE", vendor="JAI Corporation", shutter="global", colour="prism"
    )
    c.upsert_camera("U330439", "AP-3200T-PGE", firmware="0.0.3.4")
    return c


def test_schema_is_created_and_migrated_once(tmp_path: Path) -> None:
    path = tmp_path / "c.sqlite"
    with Catalog(path) as c:
        assert c.version == SCHEMA_VERSION
    with Catalog(path) as c:  # opening again does not re-run the migrations
        assert c.version == SCHEMA_VERSION
    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {
        "camera_models",
        "cameras",
        "lenses",
        "lens_stops",
        "pairs",
        "pair_stops",
        "fingerprints",
        "results",
        "change_log",
    } <= tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_lens_stops_are_picked_from_the_standard_list(cat: Catalog) -> None:
    lid = cat.add_lens("JAI 6 mm 0628-C3", focal_mm=6, mount="C", stops=[2.8, 4, 5.6, 8, 11, 16])
    assert lid == "jai_6mm_0628_c3" == lens_id_for("JAI 6 mm 0628-C3")
    lens = cat.lens(lid)
    assert lens is not None and (lens.open_f, lens.closed_f) == (2.8, 16.0)
    assert [s.end_stop for s in lens.stops] == ["open", None, None, None, None, "closed"]
    with pytest.raises(CatalogError, match="picked from"):
        cat.add_lens("Odd lens", focal_mm=12, mount="C", stops=[1.8, 4])  # 1.8 is not a mark
    with pytest.raises(CatalogError, match="mount"):
        cat.add_lens("Odd lens", focal_mm=12, mount="bayonet", stops=[2, 4])
    with pytest.raises(CatalogError, match="name"):
        cat.add_lens("   ", focal_mm=12, mount="C", stops=[2, 4])


def test_dots_are_kept_but_not_offered_as_stops(cat: Catalog) -> None:
    lid = cat.add_lens(
        "Kowa LM8HC",
        lens_id="kowa_8mm",
        focal_mm=8,
        mount="C",
        stops=[1.4, 2, 2.8, 4, 8, 16],
        dots=[5.6, 11],
    )
    cat.set_transmission(lid, 5.6, 0.093)  # measured at a dot: kept
    lens = cat.lens(lid)
    assert lens is not None and 5.6 not in [s.f_number for s in lens.stops]
    with pytest.raises(CatalogError, match="no stop f/3"):
        cat.set_transmission(lid, 3.0, 0.5)


def test_every_change_is_logged_with_old_and_new(cat: Catalog) -> None:
    cat.upsert_camera("U330439", "AP-3200T-PGE", firmware="0.0.3.5", actor="human", reason="update")
    assert not cat.upsert_camera("U330439", "AP-3200T-PGE", firmware="0.0.3.5")  # no change
    log = cat.changes(10)
    fw = [c for c in log if c["col"] == "firmware"]
    assert fw[0]["old"] == "0.0.3.4" and fw[0]["new"] == "0.0.3.5" and fw[0]["actor"] == "human"
    with pytest.raises(CatalogError, match="unknown camera model"):
        cat.upsert_camera("X1", "NOPE")


def test_runs_replace_their_results_and_trace_to_the_model(cat: Catalog) -> None:
    rows = [Result("read_noise_dn", 1.7, "DN", gain_db=0, exposure_us=1000)]
    cat.add_run(
        "U330439/a",
        "U330439",
        kind="fingerprint",
        started_utc="2026-10-25T10:00:00",
        status="pass",
        results=rows,
    )
    cat.add_run(
        "U330439/a",
        "U330439",
        kind="fingerprint",
        started_utc="2026-10-25T10:00:00",
        status="pass",
        results=rows * 2,
    )  # imported again: replaced, not added
    assert len(cat.results("U330439/a")) == 2
    cat.add_run(
        "U330439/old",
        "U330439",
        kind="fingerprint",
        started_utc="2026-10-21T10:00:00",
        status="pass",
        results=rows,
        valid=False,
    )
    got = cat.model_results("AP-3200T-PGE", "read_noise_dn")
    assert {run for run, _ in got} == {"U330439/a"}  # history-only runs are left out
    with pytest.raises(CatalogError, match="unknown camera"):
        cat.add_run("X/1", "X", kind="fingerprint", started_utc="t", status="pass", results=[])


def test_pick_list_puts_tested_lenses_first(cat: Catalog) -> None:
    a = cat.add_lens("Alpha 12 mm", focal_mm=12, mount="C", stops=[2, 16])
    b = cat.add_lens("Zeta 6 mm", focal_mm=6, mount="C", stops=[2.8, 16])
    cat.set_pair_stop("AP-3200T-PGE", b, 2.8, "G", 0.82)
    assert [x.id for x in cat.lenses_for_model("AP-3200T-PGE")] == [b, a]
    assert cat.find_lens("Zeta 6 mm, f/2.8") is not None
    assert cat.find_lens("Unknown 3 mm, f/2") is None


def _fp_doc(lens: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "created_utc": "2026-10-25T10:13:20+00:00",
        "temperatures_c": {"cap": 59.8, "finish": 61.2},
        "identity": {
            "serial": "U330439",
            "model": "AP-3200T-PGE",
            "firmware": "0.0.3.4",
            "vendor": "JAI Corporation",
            "lens": lens,
        },
        "timing": {"fps_full_frame": {"RGB12V1Packed": 8.0}, "stale_frames_after_change": 0},
        "dark": {
            "black_dn": 100.0,
            "read_noise_dn": 1.73,
            "hot_pixel_count": 3160,
            "points": [
                {
                    "gain_db": 0.0,
                    "exposure_us": 1000.0,
                    "black_dn": 100.0,
                    "read_noise_dn": 1.73,
                    "dsnu_dn": 3.5,
                }
            ],
            "repeats": {"black_dn": {"spread_pct": 1.0}},
        },
        "flat": {
            "photon_transfer": {
                "system_gain_dn_per_e": 0.8935,
                "read_noise_e": 1.9,
                "full_well_e": 4470.0,
            },
            "response": {"snr": [[3691.8, 64.2]], "linearity_error_pct": 0.21},
            "flat": {"prnu_pct": 0.68, "vignetting_corner_ratio": 0.82},
        },
        "colour": {"planes": [{"name": "G", "relative_to_green": 1.0, "corner_ratio": 0.819}]},
        "noise_gain": {
            "table": [
                {
                    "gain_db": 6.0,
                    "signal_dn": 990.0,
                    "snr": 23.7,
                    "gain_dn_per_e": 1.74,
                    "read_noise_e": 1.9,
                    "full_well_e": 2293,
                }
            ],
            "gain_modes": {
                "individual": {"gain_db": 0.0, "full_well_e": 9626},
                "signal_ratio": 0.472,
            },
        },
        "lens_iris": {
            "lens": "JAI 6 mm 0628-C3, f/5.6",
            "light_vs_diffuser_row": 0.198,
            "corner_ratio": {"G": 1.049},
        },
        "user_sets": {
            "persistence": {"kept": ["exposure_us"], "lost": ["hdr.enabled"], "slot": "UserSet1"}
        },
    }


def test_results_carry_their_conditions() -> None:
    res = results_from_fingerprint(_fp_doc("JAI 6 mm 0628-C3, f/2.8"), 2.8)  # type: ignore[arg-type]
    by = {(r.quantity, r.gain_db, r.gain_mode, r.f_number, r.plane, r.setting): r for r in res}
    assert by[("read_noise_e", 6.0, "master", None, None, None)].value == 1.9
    assert by[("full_well_e", 0.0, "individual", None, None, None)].value == 9626
    assert by[("corner_ratio", None, None, 2.8, "G", None)].value == 0.819
    assert by[("corner_ratio", None, None, 5.6, "G", None)].value == 1.049
    assert by[("user_set_kept", None, None, None, None, "hdr.enabled")].value == 0.0
    black = next(r for r in res if r.quantity == "black_dn" and r.spread_pct is not None)
    assert black.spread_pct == 1.0


def test_import_loads_lenses_cameras_runs_and_pairs(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    shutil.copytree(app_config_dir(), cfg)
    fp = tmp_path / "fingerprints" / "U330439" / "20261025T101320Z"
    fp.mkdir(parents=True)
    (fp / "fingerprint.yaml").write_text(yaml.safe_dump(_fp_doc("JAI 6 mm 0628-C3, f/2.8")))
    with Catalog(tmp_path / "c.sqlite") as c:
        rep = import_all(c, config_dir=cfg, fingerprints=tmp_path / "fingerprints")
        assert rep.lenses >= 2 and rep.cameras >= 3 and rep.runs == 2  # + the JAI live checks
        jai = c.lens("jai_6mm_0628")
        assert jai is not None and jai.tested_on == ("AP-3200T-PGE",)
        assert {s.f_number: s.transmission for s in jai.stops}[5.6] == pytest.approx(0.198)
        kowa = c.lens("kowa_8mm")
        assert kowa is not None and 11.0 not in [s.f_number for s in kowa.stops]  # a dot
        assert {
            (p["f_number"], p["plane"]) for p in c.pair_stops("AP-3200T-PGE", "jai_6mm_0628")
        } == {
            (2.8, "G"),
            (5.6, "G"),
        }
        run = next(r for r in c.runs("U330439") if r["kind"] == "fingerprint")
        assert (run["lens_id"], run["f_number"]) == ("jai_6mm_0628", 2.8)
        assert (run["temperature_min_c"], run["temperature_max_c"]) == (59.8, 61.2)
        before = len(c.changes(100_000))
        import_all(c, config_dir=cfg, fingerprints=tmp_path / "fingerprints")
        again = c.changes(100_000)[: len(c.changes(100_000)) - before]
        assert all(x["tbl"] == "results" for x in again)  # re-import changes no data


def test_cli_imports_and_lists(tmp_roots: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from typer.testing import CliRunner

    from camera_lab.cli import app

    monkeypatch.setenv("CAMERA_LAB_CATALOG", str(tmp_roots / "config" / "catalog.sqlite"))
    run = CliRunner().invoke
    res = run(app, ["two-d", "catalog", "import", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["result"]["lenses"] >= 2
    res = run(app, ["two-d", "catalog", "lenses", "--model", "AP-3200T-PGE"])
    assert res.exit_code == 0 and "jai_6mm_0628: JAI 6 mm 0628-C3" in res.stdout
    res = run(app, ["two-d", "catalog", "changes", "--limit", "5"])
    assert res.exit_code == 0 and "import" in res.stdout


def test_sheet_cells_round_trip_and_sections_split() -> None:
    from camera_lab.two_d.catalog.papers import flatten, split_sections, unflatten

    doc = {
        "model": "X",
        "performance": {"black_dn": 100, "snr_vs_signal": {"points": {0.5: 40}}},
        "limitations": ["a", "b"],
        "empty": {},
    }
    cells = flatten(doc)
    assert cells["performance/black_dn"] == 100 and cells["limitations"] == ["a", "b"]
    assert unflatten(cells) == {
        **doc,
        "performance": {"black_dn": 100, "snr_vs_signal": {"points": {"0.5": 40}}},
    }  # keys as in JSON
    md = "# Title (v1)\nintro\n\n## 1. Summary\ntext\n### sub\nmore\n## 2. Next\nend\n"
    assert split_sections(md) == [
        ("", "# Title (v1)\nintro"),
        ("1. Summary", "text\n### sub\nmore"),
        ("2. Next", "end"),
    ]


def test_papers_are_generated_from_the_catalog(tmp_path: Path) -> None:
    """The hand-written papers become rows and cells; the generated sheet gives the tools the
    same values; a fingerprint updates the cells the tools read; edits are logged."""
    from camera_lab.core.config import load_yaml_model
    from camera_lab.two_d.camera_models import ModelSheet, load_model_sheet
    from camera_lab.two_d.catalog.papers import import_papers, update_facts_from_run, write_papers

    cfg = tmp_path / "config"
    shutil.copytree(app_config_dir(), cfg)
    models = cfg / "camera_models"  # the hand-written layout, as before the migration
    for name in ("camera.md", "camera.yaml"):
        src = models / "AP-3200T-PGE" / name
        (models / f"AP-3200T-PGE{src.suffix}").write_text(src.read_text())
    shutil.rmtree(models / "AP-3200T-PGE")
    fp = tmp_path / "fingerprints" / "U330439" / "20261025T101320Z"
    fp.mkdir(parents=True)
    (fp / "fingerprint.yaml").write_text(yaml.safe_dump(_fp_doc("JAI 6 mm 0628-C3, f/2.8")))
    out = tmp_path / "papers"
    with Catalog(tmp_path / "c.sqlite") as c:
        import_all(c, config_dir=cfg, fingerprints=tmp_path / "fingerprints")
        facts = import_papers(c, cfg)
        assert facts["AP-3200T-PGE"] > 100
        files = write_papers(c, "AP-3200T-PGE", out)
        assert {f.name for f in files} == {
            "camera.md",
            "camera.yaml",
            "jai_6mm_0628.md",
            "jai_6mm_0628.yaml",
        }
        old = load_yaml_model(cfg / "camera_models" / "AP-3200T-PGE.yaml", ModelSheet)
        assert load_model_sheet("AP-3200T-PGE", out) == old  # what the tools read is unchanged
        md = (out / "AP-3200T-PGE" / "camera.md").read_text()
        assert md.startswith("# JAI Apex AP-3200T-PGE — camera white paper (generated ")
        assert "## 1. Summary" in md and "Measured data (generated from the catalog)" in md
        assert "| 20261025T101320Z | 100 | 1.73 |" in md
        pair = (out / "AP-3200T-PGE" / "lenses" / "jai_6mm_0628.md").read_text()
        assert "| f/5.6 | - | 0.198 |" in pair
        changed = update_facts_from_run(c, "AP-3200T-PGE", "U330439/20261025T101320Z")
        assert "performance/read_noise_dn" in changed
        assert c.facts("AP-3200T-PGE")["performance/read_noise_dn"] == 1.73
        c.set_fact("AP-3200T-PGE", "performance/black_dn", 101, source="manual")
        write_papers(c, "AP-3200T-PGE", out)
        sheet = load_model_sheet("AP-3200T-PGE", out)
        assert sheet is not None and sheet.performance.black_dn == 101
        log = [x for x in c.changes(50) if x["tbl"] == "model_facts" and x["col"] == "value"]
        assert any(x["row_key"] == "AP-3200T-PGE/performance/black_dn" for x in log)


def test_a_new_model_gets_a_sheet_the_tools_can_load(tmp_path: Path) -> None:
    """TRI089S-M 2026-10-27: no hand-written paper, so the fingerprint must give the sheet
    its identity cells (model, design mode, sensor, full scale) as well as the numbers."""
    from camera_lab.two_d.camera_models import load_model_sheet
    from camera_lab.two_d.catalog.papers import update_facts_from_run, write_papers

    with Catalog(tmp_path / "c.sqlite") as c:
        c.upsert_camera_model(
            "NEW-1", vendor="Lucid", shutter="global", colour="mono", design_mode="raw"
        )
        c.upsert_camera("S1", "NEW-1")
        doc = _fp_doc("Kowa LM8HC, f/1.4")
        doc["flat"]["response"]["saturation_dn"] = 4095.0  # type: ignore[index]
        c.add_run(
            "S1/a",
            "S1",
            kind="fingerprint",
            started_utc="2026-10-27T08:36:23",
            status="pass",
            results=results_from_fingerprint(doc, 1.4),
        )  # type: ignore[arg-type]
        update_facts_from_run(c, "NEW-1", "S1/a", {"gain_db": 0.0, "black_level": 1.0})
        write_papers(c, "NEW-1", tmp_path / "papers")
    sheet = load_model_sheet("NEW-1", tmp_path / "papers")
    assert sheet is not None and sheet.design_mode == "raw" and sheet.sensor.colour == "mono"
    assert sheet.performance.full_scale_dn == 4095 and sheet.performance.read_noise_dn == 1.73
