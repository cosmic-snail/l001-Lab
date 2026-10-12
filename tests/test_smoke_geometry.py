"""Tests for scripts/smoke_geometry.py against the frozen smoke-v1 fixture."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "smoke_geometry.py"
CONFIG_PATH = REPO_ROOT / "configs" / "smoke_v1.json"


def _load_smoke():
    spec = importlib.util.spec_from_file_location("smoke_geometry", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_geometry"] = module
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke()


def _reject_constant(name):
    raise AssertionError(f"report contains the non-finite JSON constant {name}")


def _run(config_path, out_dir):
    return smoke.main(["--config", str(config_path), "--out", str(out_dir)])


def _read_report(out_dir):
    text = (out_dir / "report.json").read_text(encoding="utf-8")
    return json.loads(text, parse_constant=_reject_constant)


def _frozen_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def config_copy(tmp_path):
    """A writable copy of the frozen config, so tests never touch the repo file."""
    copy = tmp_path / "smoke_v1.json"
    copy.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return copy


def _write_mutated_config(tmp_path, mutate):
    config = _frozen_config()
    mutate(config)
    path = tmp_path / "mutated_config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_healthy_workflow_passes_with_analytic_and_roundtrip_checks(tmp_path, config_copy):
    out_dir = tmp_path / "run-healthy"
    assert _run(config_copy, out_dir) == 0
    report = _read_report(out_dir)
    assert report["status"] == "PASS"
    assert report["error"] is None
    assert report["summary"]["checks_failed"] == []
    checks = {check["name"]: check for check in report["checks"]}
    assert all(check["pass"] for check in report["checks"])
    assert checks["stock.volume_mm3"]["expected"] == pytest.approx(3200.0)
    assert checks["cavity.volume_mm3"]["expected"] == pytest.approx(144.0)
    assert checks["final.volume_mm3"]["expected"] == pytest.approx(3056.0)
    assert checks["final.volume_mm3"]["observed"] == pytest.approx(3056.0, abs=0.001)
    assert checks["final.removal_volume_mm3"]["observed"] == pytest.approx(144.0, abs=0.001)
    assert checks["final.removal_volume_mm3"]["tolerance"] == pytest.approx(max(0.001, 1e-6 * 3200.0))
    assert checks["imported.solid_count"]["pass"] is True
    assert checks["imported.volume_mm3"]["pass"] is True
    for key in ("xmin", "ymin", "zmin", "xmax", "ymax", "zmax"):
        roundtrip = checks[f"imported.bbox_{key}_mm"]
        assert roundtrip["pass"] is True
        assert roundtrip["tolerance"] == pytest.approx(0.001)
        assert roundtrip["abs_error"] <= 0.001
    assert report["analytic"]["stock_volume_mm3"] == pytest.approx(3200.0)
    assert report["analytic"]["cavity_volume_mm3"] == pytest.approx(144.0)
    assert report["analytic"]["final_volume_mm3"] == pytest.approx(3056.0)
    assert report["analytic"]["stock_bbox_mm"] == [0.0, 0.0, -10.0, 20.0, 16.0, 0.0]
    assert report["units"] == "mm"
    assert report["schema_version"] == "smoke-v1"
    assert report["source_protocol"]["document"].endswith("I001A_executable_pilot_plan.md")
    assert report["source_protocol"]["version"] == "1.0"
    assert report["fixture"]["cavity_kind"] == "top_blind_rectangular"
    step = report["artifacts"]["step"]
    assert step["exists"] is True
    assert step["size_bytes"] > 0
    assert step["size_bytes"] == (out_dir / "final.step").stat().st_size
    assert len(step["sha256"]) == 64
    assert len(report["config"]["sha256"]) == 64
    assert len(report["script"]["sha256"]) == 64
    assert report["config"]["snapshot"]["stock"]["intervals"]["x"] == [0, 20]
    assert report["environment"]["timestamp_utc"].endswith("+00:00")
    assert report["environment"]["python_version"].startswith("3.")
    assert report["versions"]["numpy"] != "unknown"
    assert report["versions"]["cadquery"] != "unknown"
    assert report["command"]["argv"] == ["--config", str(config_copy), "--out", str(out_dir)]
    for stage in ("import", "build", "cut", "export", "reimport", "check", "total"):
        timing = report["timings"][stage]
        assert timing is not None
        assert timing["wall_s"] >= 0.0
        assert timing["cpu_s"] >= 0.0
    assert report["timings"]["total"]["wall_s"] > 0.0


def test_corrupted_step_artifact_is_non_pass(tmp_path, config_copy, monkeypatch):
    out_dir = tmp_path / "run-corrupt"

    def corrupt_export(shape, path):
        Path(path).write_bytes(b"ISO-10303-21;\nnot a real step file\n")

    monkeypatch.setattr(smoke, "export_step", corrupt_export)
    assert _run(config_copy, out_dir) != 0
    report = _read_report(out_dir)
    assert report["status"] in {"FAIL", "ERROR"}
    assert report["artifacts"]["step"]["size_bytes"] > 0
    assert report["error"] is not None or any(not check["pass"] for check in report["checks"])


def test_injected_import_exception_is_error_with_type_and_message(tmp_path, config_copy, monkeypatch):
    out_dir = tmp_path / "run-import-error"

    def broken_import(path):
        raise RuntimeError("injected STEP import failure")

    monkeypatch.setattr(smoke, "import_step", broken_import)
    assert _run(config_copy, out_dir) != 0
    report = _read_report(out_dir)
    assert report["status"] == "ERROR"
    assert report["error"]["type"] == "RuntimeError"
    assert "injected STEP import failure" in report["error"]["message"]
    assert (out_dir / "final.step").is_file()


def test_shifted_roundtrip_geometry_fails_the_bbox_gate(tmp_path, config_copy, monkeypatch):
    import cadquery as cq

    def shifted_import(path):
        # Same analytic volume as the final shape, top extremum shifted from z=0 to z=-0.45.
        box = cq.Workplane("XY").box(20.0, 16.0, 9.55, centered=False)
        return box.translate((0.0, 0.0, -10.0)).val()

    out_dir = tmp_path / "run-shifted"
    monkeypatch.setattr(smoke, "import_step", shifted_import)
    assert _run(config_copy, out_dir) != 0
    report = _read_report(out_dir)
    assert report["status"] == "FAIL"
    assert report["error"] is None
    failed = {check["name"] for check in report["checks"] if not check["pass"]}
    assert "imported.bbox_zmax_mm" in failed
    assert "imported.volume_mm3" not in failed


def test_undersized_roundtrip_geometry_fails_the_volume_gate(tmp_path, config_copy, monkeypatch):
    import cadquery as cq

    def undersized_import(path):
        box = cq.Workplane("XY").box(10.0, 8.0, 7.5, centered=False)
        return box.translate((0.0, 0.0, -10.0)).val()

    out_dir = tmp_path / "run-undersized"
    monkeypatch.setattr(smoke, "import_step", undersized_import)
    assert _run(config_copy, out_dir) != 0
    report = _read_report(out_dir)
    assert report["status"] == "FAIL"
    failed = {check["name"] for check in report["checks"] if not check["pass"]}
    assert {"imported.volume_mm3", "imported.bbox_xmax_mm", "imported.bbox_zmax_mm"} <= failed


def test_extra_imported_root_fails_the_solid_count_gate(tmp_path, config_copy, monkeypatch):
    import cadquery as cq

    real_import_step = cq.importers.importStep

    def import_with_extra_root(path):
        imported = real_import_step(str(path))
        extra = cq.Workplane("XY").box(2.0, 2.0, 2.0, centered=False)
        return imported.add(extra.translate((100.0, 100.0, 100.0)))

    out_dir = tmp_path / "run-extra-root"
    monkeypatch.setattr(cq.importers, "importStep", import_with_extra_root)
    assert _run(config_copy, out_dir) != 0
    report = _read_report(out_dir)
    assert report["status"] == "FAIL"
    failed = {check["name"] for check in report["checks"] if not check["pass"]}
    assert "imported.solid_count" in failed


def _set_nonfinite_interval(config):
    config["stock"]["intervals"]["z"] = [-10, float("inf")]


def _set_inches(config):
    config["units"] = "inch"


def _set_through_cavity(config):
    config["cavity"]["intervals"]["z"] = [-10, 0]


def _move_cavity_outside_stock(config):
    config["cavity"]["intervals"]["x"] = [18, 26]


def _set_zero_extent_stock(config):
    config["stock"]["intervals"]["y"] = [4, 4]


def _set_relaxed_bbox_tolerance(config):
    config["tolerances"]["bbox_extremum_mm"] = 0.002


def _set_tightened_bbox_tolerance(config):
    config["tolerances"]["bbox_extremum_mm"] = 0.0005


def _set_relaxed_volume_abs_tolerance(config):
    config["tolerances"]["volume_abs_mm3"] = 0.01


def _set_tightened_volume_abs_tolerance(config):
    config["tolerances"]["volume_abs_mm3"] = 0.0001


def _set_relaxed_volume_relative_tolerance(config):
    config["tolerances"]["volume_relative"] = 1e-5


def _set_tightened_volume_relative_tolerance(config):
    config["tolerances"]["volume_relative"] = 1e-7


def _set_changed_protocol_version(config):
    config["source_protocol"]["version"] = "1.1"


@pytest.mark.parametrize(
    "mutate",
    [_set_relaxed_bbox_tolerance, _set_tightened_bbox_tolerance,
     _set_relaxed_volume_abs_tolerance, _set_tightened_volume_abs_tolerance,
     _set_relaxed_volume_relative_tolerance, _set_tightened_volume_relative_tolerance,
     _set_changed_protocol_version],
)
def test_non_frozen_tolerance_or_protocol_is_rejected_before_output(tmp_path, mutate):
    config_path = _write_mutated_config(tmp_path, mutate)
    out_dir = tmp_path / "run-nonfrozen-config"
    assert _run(config_path, out_dir) != 0
    assert not out_dir.exists()


@pytest.mark.parametrize(
    "mutate",
    [_set_nonfinite_interval, _set_inches, _set_through_cavity, _move_cavity_outside_stock,
     _set_zero_extent_stock],
)
def test_invalid_config_is_rejected_before_any_output(tmp_path, mutate):
    config_path = _write_mutated_config(tmp_path, mutate)
    out_dir = tmp_path / "run-invalid-config"
    assert _run(config_path, out_dir) != 0
    assert not out_dir.exists()


def test_existing_output_directory_is_refused_without_overwrite(tmp_path, config_copy):
    out_dir = tmp_path / "run-existing"
    out_dir.mkdir()
    sentinel = out_dir / "keep.txt"
    sentinel.write_text("existing evidence", encoding="utf-8")
    assert _run(config_copy, out_dir) != 0
    assert sentinel.read_text(encoding="utf-8") == "existing evidence"
    assert not (out_dir / "report.json").exists()
    assert not (out_dir / "final.step").exists()
