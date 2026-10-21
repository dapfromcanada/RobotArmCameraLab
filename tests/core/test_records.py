"""Tests for core.records: run folder layout, bit-exact TIFF round trip, schemas."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
import tifffile
from pydantic import BaseModel, ValidationError

from camera_lab.core import records
from camera_lab.core.paths import UnsafePathError
from camera_lab.core.records import (
    CaptureLimitError,
    CaptureSidecar,
    RecordError,
    RunNotFoundError,
    RunRecord,
    RunRecorder,
    SoftwareInfo,
    list_runs,
    load_run,
    parse_capture_id,
    read_capture,
)

CAMERA = {
    "vendor": "camera_lab",
    "model": "Simulated2D",
    "serial": "SIM-0001",
    "firmware": "1.0",
}
HASH = "a" * 64
SOFTWARE = SoftwareInfo(camera_lab="0.1.0", sdk_name="sim", sdk_version="1", firmware="1.0")


class FakeNow:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 21, 10, 15, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.t += timedelta(milliseconds=10)
        return self.t


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "runs"


@pytest.fixture
def make_run(root: Path) -> Callable[..., RunRecorder]:
    def _make(**kw: Any) -> RunRecorder:
        args: dict[str, Any] = {
            "camera_info": CAMERA,
            "method": "manual",
            "operator": "human",
            "goal": "Check exposure on the bin",
            "max_steps": 10,
            "software": SOFTWARE,
            "limits_hash": HASH,
            "root": root,
            "now": FakeNow(),
        }
        args.update(kw)
        return RunRecorder.create(**args)

    return _make


def _add(rec: RunRecorder, img: np.ndarray, **kw: Any) -> str:
    args: dict[str, Any] = {
        "pixel_format": "Mono8",
        "bit_depth": 8,
        "settings": {"exposure_us": 1000.0, "gain_db": 0.0},
        "lights": {"white_ring": 40.0},
        "scores": {"mean": 0.4, "composite": 0.8},
        "frame_id": 7,
    }
    args.update(kw)
    return rec.add_capture(img, **args)


# ------------------------------------------------------------------ layout
def test_create_layout(make_run: Callable[..., RunRecorder], root: Path) -> None:
    rec = make_run()
    assert rec.run_id == "20261021-101500_SIM-0001_check-exposure-on-the-bin"
    d = root / rec.run_id
    assert rec.run_dir == d.resolve()
    assert sorted(p.name for p in d.iterdir()) == [
        "captures",
        "previews",
        "run.json",
        "steps.jsonl",
    ]
    data = json.loads((d / "run.json").read_text())
    RunRecord.model_validate(data)
    assert data["schema_version"] == 1
    assert data["finished_at"] is None
    assert data["limits_hash"] == HASH
    assert data["software"]["camera_lab"] == "0.1.0"
    assert data["camera"] == CAMERA
    assert data["started_at"].endswith("Z") or data["started_at"].endswith("+00:00")


def test_run_id_collision_gets_suffix(make_run: Callable[..., RunRecorder]) -> None:
    a = make_run(goal="x")
    b = make_run(goal="x")
    assert a.run_id.endswith("_x")
    assert b.run_id.endswith("_x-2")


def test_slug_source_and_default(make_run: Callable[..., RunRecorder]) -> None:
    assert make_run(goal="", slug_source=None).run_id.endswith("_run")
    assert make_run(slug_source="Focus Test").run_id.endswith("_focus-test")


def test_camera_object_with_model_dump(make_run: Callable[..., RunRecorder]) -> None:
    class Info(BaseModel):
        vendor: str
        serial: str

    rec = make_run(camera_info=Info(vendor="v", serial="U330439"))
    assert "_U330439_" in rec.run_id


@pytest.mark.parametrize("serial", ["../x", "a_b", "", "a/b"])
def test_bad_serial_rejected(make_run: Callable[..., RunRecorder], serial: str, root: Path) -> None:
    with pytest.raises(UnsafePathError):
        make_run(camera_info={"serial": serial})


def test_invalid_record_leaves_no_folder(make_run: Callable[..., RunRecorder], root: Path) -> None:
    with pytest.raises(ValidationError):
        make_run(method="bogus")
    assert list(root.iterdir()) == []


# --------------------------------------------------------------- round trip
@pytest.mark.parametrize(
    ("dtype", "shape", "pf", "bits"),
    [
        (np.uint8, (48, 64), "Mono8", 8),
        (np.uint16, (48, 64), "Mono12", 12),
        (np.uint8, (48, 64), "BayerRG8", 8),
        (np.uint8, (48, 64, 3), "RGB8", 8),
        (np.uint16, (48, 64, 3), "RGB16", 16),
    ],
)
def test_capture_round_trip_bit_exact(
    make_run: Callable[..., RunRecorder],
    root: Path,
    dtype: Any,
    shape: tuple[int, ...],
    pf: str,
    bits: int,
) -> None:
    rng = np.random.default_rng(0)
    hi = 2**bits
    img = rng.integers(0, hi, size=shape, dtype=np.uint64).astype(dtype)
    rec = make_run()
    cid = _add(rec, img, pixel_format=pf, bit_depth=bits)
    assert cid == f"{rec.run_id}/0001"
    back, side = read_capture(cid, root=root)
    assert back.dtype == img.dtype
    assert back.shape == img.shape
    assert np.array_equal(back, img)
    assert side.pixel_format == pf and side.bit_depth == bits
    assert (side.width, side.height) == (64, 48)
    assert side.channels == (1 if len(shape) == 2 else 3)
    # sidecar file validates and matches the TIFF ImageDescription
    side_file = rec.run_dir / "captures" / "0001.json"
    CaptureSidecar.model_validate_json(side_file.read_bytes())
    with tifffile.TiffFile(rec.run_dir / "captures" / "0001.tiff") as tf:
        page = tf.pages[0]
        assert page.compression == tifffile.COMPRESSION.NONE
        desc = json.loads(page.description)
    assert CaptureSidecar.model_validate(desc) == side


def test_capture_numbering_and_rereading(make_run: Callable[..., RunRecorder], root: Path) -> None:
    rec = make_run()
    img = np.zeros((4, 4), np.uint8)
    ids = [_add(rec, img) for _ in range(3)]
    assert [parse_capture_id(i)[1] for i in ids] == [1, 2, 3]
    again = RunRecorder.load(rec.run_id, root=root)
    assert again.capture_count == 3
    assert _add(again, img).endswith("/0004")
    _arr, side = again.read_capture(ids[0])
    assert side.frame_id == 7


def test_non_ascii_note_is_escaped_in_tiff(make_run: Callable[..., RunRecorder]) -> None:
    rec = make_run()
    _add(rec, np.zeros((4, 4), np.uint8), note="Grün ✓")
    _, side = rec.read_capture(f"{rec.run_id}/0001")
    assert side.note == "Grün ✓"


def test_preview_written_downscaled(make_run: Callable[..., RunRecorder]) -> None:
    rec = make_run()
    img = np.zeros((1500, 3000), np.uint8)
    prev = np.zeros((1500, 3000, 3), np.uint8)
    prev[..., 0] = 255  # red in RGB
    _add(rec, img, preview=prev)
    jpg = rec.run_dir / "previews" / "0001.jpg"
    decoded = cv2.imread(str(jpg))
    assert decoded.shape == (512, 1024, 3)
    assert decoded[..., 2].mean() > 200  # BGR: red channel last
    _, side = rec.read_capture(f"{rec.run_id}/0001")
    assert side.preview == "previews/0001.jpg"


def test_small_gray_preview(make_run: Callable[..., RunRecorder]) -> None:
    rec = make_run()
    _add(rec, np.zeros((8, 8), np.uint8), preview=np.full((8, 8), 128, np.uint8))
    assert cv2.imread(str(rec.run_dir / "previews" / "0001.jpg")).shape[:2] == (8, 8)


@pytest.mark.parametrize(
    "bad",
    [
        np.zeros((4, 4), np.float32),
        np.zeros((4, 4, 4), np.uint8),
        np.zeros((0, 4), np.uint8),
        np.zeros(4, np.uint8),
        [[1, 2]],
    ],
)
def test_bad_images_rejected(make_run: Callable[..., RunRecorder], bad: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        _add(make_run(), bad)


@pytest.mark.parametrize("bad", [np.zeros((4, 4), np.uint16), np.zeros((4, 4, 2), np.uint8)])
def test_bad_preview_rejected(make_run: Callable[..., RunRecorder], bad: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        _add(make_run(), np.zeros((4, 4), np.uint8), preview=bad)


def test_capture_limit(make_run: Callable[..., RunRecorder]) -> None:
    rec = make_run(max_captures=2)
    img = np.zeros((2, 2), np.uint8)
    _add(rec, img)
    _add(rec, img)
    with pytest.raises(CaptureLimitError, match="limit 2"):
        _add(rec, img)


def test_no_temp_files_left(make_run: Callable[..., RunRecorder]) -> None:
    rec = make_run()
    _add(rec, np.zeros((4, 4), np.uint8), preview=np.zeros((4, 4, 3), np.uint8))
    rec.finish(best_capture_id=f"{rec.run_id}/0001")
    leftovers = [p for p in rec.run_dir.rglob("*") if p.name.startswith(".")]
    assert leftovers == []


def test_atomic_write_cleans_up_on_failure(tmp_path: Path) -> None:
    tmp_path = tmp_path / "atomic"
    tmp_path.mkdir()
    target = tmp_path / "f.bin"
    target.write_bytes(b"old")
    with pytest.raises(RuntimeError), records._atomic_open(target) as fh:
        fh.write(b"partial")
        raise RuntimeError("disk full")
    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["f.bin"]


# ------------------------------------------------------------- steps/finish
def test_log_steps_and_finish(make_run: Callable[..., RunRecorder], root: Path) -> None:
    rec = make_run(method="agent", operator="agent")
    rec.log_step(
        "agent",
        "set_camera_settings",
        {"gain_db": 30},
        {"ok": False},
        ["gain_db 30.0 > limit 18.0"],
    )
    _add(rec, np.zeros((4, 4), np.uint8))
    rec.log_step("agent", "capture", {"count": 1}, {"capture_ids": [f"{rec.run_id}/0001"]})
    lines = (rec.run_dir / "steps.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["rejections"] == ["gain_db 30.0 > limit 18.0"]
    steps = rec.read_steps()
    assert steps[1].tool == "capture"

    final = rec.finish(
        best_capture_id=f"{rec.run_id}/0001", summary="done", preset_saved="p.yaml", steps_used=2
    )
    assert final.finished_at is not None
    assert final.steps_used == 2
    on_disk = load_run(rec.run_id, root=root)
    assert on_disk == final
    with pytest.raises(RecordError, match="already finished"):
        rec.finish(best_capture_id=None)


@pytest.mark.parametrize("best", ["20261021-101500_SIM-0001_other/0001", "SUFFIX/0009"])
def test_finish_rejects_unknown_best(make_run: Callable[..., RunRecorder], best: str) -> None:
    rec = make_run()
    _add(rec, np.zeros((2, 2), np.uint8))
    with pytest.raises((RecordError, ValueError)):
        rec.finish(best_capture_id=best)


def test_finish_rejects_missing_capture_number(make_run: Callable[..., RunRecorder]) -> None:
    rec = make_run()
    with pytest.raises(RecordError):
        rec.finish(best_capture_id=f"{rec.run_id}/0005")


# --------------------------------------------------------------- readers
def test_list_runs_filters_and_best_score(make_run: Callable[..., RunRecorder], root: Path) -> None:
    a = make_run(goal="a")
    _add(a, np.zeros((2, 2), np.uint8))
    a.finish(best_capture_id=f"{a.run_id}/0001")
    make_run(goal="b", method="classic", camera_info={**CAMERA, "serial": "U330439"})
    (root / "not-a-run").mkdir()
    (root / "20261021-101500_SIM-0001_broken").mkdir()  # no run.json -> skipped
    runs = list_runs(root=root)
    assert {r.run_id.rsplit("_", 1)[1] for r in runs} == {"a", "b"}
    by = {r.run_id.rsplit("_", 1)[1]: r for r in runs}
    assert by["a"].best_score == 0.8 and by["a"].capture_count == 1
    assert by["b"].best_score is None
    assert [r.method for r in list_runs(root=root, method="classic")] == ["classic"]
    assert [r.camera_serial for r in list_runs(root=root, camera="U330439")] == ["U330439"]
    assert list_runs(root=root / "missing") == []


def test_list_runs_default_root_uses_env(tmp_roots: Path) -> None:
    assert list_runs() == []


@pytest.mark.parametrize(
    "bad",
    ["../etc", "20261021-101500_SIM-0001_x/../../y", "x", "", "20261021-101500_SIM-0001_nope"],
)
def test_load_bad_or_missing_run(root: Path, bad: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(RunNotFoundError):
        load_run(bad, root=root)


@pytest.mark.parametrize("bad", ["x/0001", "../0001", "20261021-101500_SIM-0001_a/1", 5])
def test_read_capture_bad_ids(root: Path, bad: Any) -> None:
    with pytest.raises(RunNotFoundError):
        read_capture(bad, root=root)


def test_read_capture_missing(make_run: Callable[..., RunRecorder], root: Path) -> None:
    rec = make_run()
    with pytest.raises(RunNotFoundError):
        read_capture(f"{rec.run_id}/0001", root=root)
    with pytest.raises(RunNotFoundError):
        rec.read_capture("20261021-101500_SIM-0001_other/0001")


def test_corrupt_run_json(make_run: Callable[..., RunRecorder], root: Path) -> None:
    rec = make_run()
    (rec.run_dir / "run.json").write_text('{"schema_version": 1}')
    with pytest.raises(RecordError, match=r"invalid run\.json"):
        load_run(rec.run_id, root=root)


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError):
        RunRecord(
            run_id="20261021-101500_SIM-0001_x",
            camera=CAMERA,
            method="manual",
            operator="human",
            started_at=datetime(2026, 1, 1),
            max_steps=1,
            software=SOFTWARE,
            limits_hash=HASH,
        )


def test_run_record_requires_camera_serial() -> None:
    with pytest.raises(ValidationError, match=r"camera\.serial"):
        RunRecord(
            run_id="20261021-101500_SIM-0001_x",
            camera={"vendor": "x"},
            method="manual",
            operator="human",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            max_steps=1,
            software=SOFTWARE,
            limits_hash=HASH,
        )
