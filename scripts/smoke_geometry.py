#!/usr/bin/env python3
"""Environment smoke step 1 for the frozen smoke-v1 fixture.

Builds a rectangular stock and a rectangular top blind cavity with CadQuery,
subtracts the cavity, exports STEP, reimports the artifact, and checks stock,
cavity, final, and reimported shapes against volumes and bounding boxes derived
independently from the configured intervals. Environment smoke only: no Q3, G0,
toolpath, candidate generation, or CAM behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import math
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "smoke-v1"
UNITS = "mm"
SOURCE_PROTOCOL_DOCUMENT = "docs/I001A_executable_pilot_plan.md"
SOURCE_PROTOCOL_VERSION = "1.0"
FROZEN_TOLERANCES = {"bbox_extremum_mm": 0.001, "volume_abs_mm3": 0.001,
                     "volume_relative": 1e-6}
AXES = ("x", "y", "z")
BBOX_KEYS = ("xmin", "ymin", "zmin", "xmax", "ymax", "zmax")
STAGES = ("import", "build", "cut", "export", "reimport", "check")
FRAME = {"handedness": "right-handed", "axes": ["X", "Y", "Z"], "z_axis": "up"}

_CQ = None
_NP = None


class ConfigError(ValueError):
    """The smoke-v1 config violates the frozen schema or fixture constraints."""


def import_libraries():
    """Import CadQuery, OCP, and NumPy once; return the cached modules."""
    global _CQ, _NP
    if _CQ is None:
        _CQ = importlib.import_module("cadquery")
        importlib.import_module("OCP")
    if _NP is None:
        _NP = importlib.import_module("numpy")
    return _CQ, _NP


def _number(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: expected a number, got {type(value).__name__}")
    number = float(value)  # huge JSON integers raise OverflowError, reported as invalid config
    if not math.isfinite(number):
        raise ConfigError(f"{where}: expected a finite number, got {value!r}")
    return number


def _interval(raw, where):
    if not isinstance(raw, list) or len(raw) != 2:
        raise ConfigError(f"{where}: expected [min, max]")
    low, high = _number(raw[0], f"{where}[0]"), _number(raw[1], f"{where}[1]")
    if high <= low:
        raise ConfigError(f"{where}: expected a positive interval, got [{low}, {high}]")
    return [low, high]


def _intervals(section, where):
    raw = section.get("intervals") if isinstance(section, dict) else None
    if not isinstance(raw, dict) or set(raw) != set(AXES):
        raise ConfigError(f"{where}.intervals: expected exactly the x, y, z intervals")
    return {axis: _interval(raw[axis], f"{where}.intervals.{axis}") for axis in AXES}


def validate_config(raw):
    """Return the normalized smoke-v1 config, or raise ConfigError."""
    if not isinstance(raw, dict):
        raise ConfigError("config root must be an object")
    for key, expected in (("schema_version", SCHEMA_VERSION), ("units", UNITS),
                          ("coordinate_system", FRAME)):
        if raw.get(key) != expected:
            raise ConfigError(f"{key} must be {expected!r}")
    protocol = raw.get("source_protocol")
    if protocol != {"document": SOURCE_PROTOCOL_DOCUMENT, "version": SOURCE_PROTOCOL_VERSION}:
        raise ConfigError(
            f"source_protocol must be exactly {SOURCE_PROTOCOL_DOCUMENT!r} version {SOURCE_PROTOCOL_VERSION!r}"
        )
    stock = _intervals(raw.get("stock"), "stock")
    cavity_section = raw.get("cavity")
    if not isinstance(cavity_section, dict) or cavity_section.get("kind") != "top_blind_rectangular":
        raise ConfigError("cavity must be an object of kind 'top_blind_rectangular'")
    cavity = _intervals(cavity_section, "cavity")
    for axis in ("x", "y"):
        if not (stock[axis][0] < cavity[axis][0] and cavity[axis][1] < stock[axis][1]):
            raise ConfigError(f"cavity.{axis} must lie strictly inside stock.{axis}")
    if cavity["z"][1] != stock["z"][1]:
        raise ConfigError("cavity must reach the stock top face (equal z maxima)")
    if cavity["z"][0] <= stock["z"][0]:
        raise ConfigError("cavity must be blind (z min above the stock z min)")
    tol = raw.get("tolerances")
    if not isinstance(tol, dict):
        raise ConfigError("tolerances must be an object")
    tolerances = {name: _number(tol.get(name), f"tolerances.{name}")
                  for name in ("bbox_extremum_mm", "volume_abs_mm3", "volume_relative")}
    if any(value <= 0 for value in tolerances.values()):
        raise ConfigError("tolerances must be positive")
    for name, frozen in FROZEN_TOLERANCES.items():
        if tolerances[name] != frozen:
            raise ConfigError(f"tolerances.{name} must be exactly {frozen!r}")
    return {
        "schema_version": SCHEMA_VERSION,
        "source_protocol": {"document": protocol["document"], "version": protocol["version"]},
        "units": UNITS,
        "coordinate_system": {"handedness": "right-handed", "axes": ["X", "Y", "Z"], "z_axis": "up"},
        "stock": {"intervals": stock},
        "cavity": {"kind": "top_blind_rectangular", "intervals": cavity},
        "tolerances": tolerances,
    }


def box_volume(intervals):
    return ((intervals["x"][1] - intervals["x"][0])
            * (intervals["y"][1] - intervals["y"][0])
            * (intervals["z"][1] - intervals["z"][0]))


def bbox_of(intervals):
    return [intervals[axis][0] for axis in AXES] + [intervals[axis][1] for axis in AXES]


def analytic_values(config):
    """Analytic fixture values derived from the configured intervals."""
    stock, cavity = config["stock"]["intervals"], config["cavity"]["intervals"]
    stock_volume, cavity_volume = box_volume(stock), box_volume(cavity)
    return {
        "stock_volume_mm3": stock_volume,
        "cavity_volume_mm3": cavity_volume,
        "final_volume_mm3": stock_volume - cavity_volume,
        "stock_bbox_mm": bbox_of(stock),
        "cavity_bbox_mm": bbox_of(cavity),
        "final_bbox_mm": bbox_of(stock),
        "derivation": "ordinary arithmetic on the configured bounding intervals",
    }


def build_box(intervals):
    """Solid spanning the intervals: positive extents boxed, then translated."""
    dims = [intervals[axis][1] - intervals[axis][0] for axis in AXES]
    origin = tuple(intervals[axis][0] for axis in AXES)
    workplane = _CQ.Workplane("XY").box(dims[0], dims[1], dims[2], centered=False)
    return workplane.translate(origin).val()


def build_fixture(config):
    return build_box(config["stock"]["intervals"]), build_box(config["cavity"]["intervals"])


def cut_shapes(stock, cavity):
    return stock.cut(cavity)


def export_step(shape, path):
    """Export a shape to STEP; the .step extension selects the writer."""
    _CQ.exporters.export(shape, str(path))


def import_step(path):
    """Reimport the STEP artifact; never returns the pre-export shape."""
    roots = list(_CQ.importers.importStep(str(path)).vals())
    return roots[0] if len(roots) == 1 else _CQ.Compound.makeCompound(roots)


def measure_shape(shape):
    """Solid count, validity, volume, and bounding box for one shape."""
    solids = shape.Solids()
    metrics = {"solid_count": len(solids), "valid": bool(shape.isValid()), "volume": None, "bbox": None}
    if len(solids) == 1:
        metrics["volume"] = float(shape.Volume())
        box = shape.BoundingBox()
        metrics["bbox"] = [float(box.xmin), float(box.ymin), float(box.zmin),
                           float(box.xmax), float(box.ymax), float(box.zmax)]
    return metrics


def finite_number(value):
    """value as a finite float, or None when it is not a finite real number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def make_check(name, expected, observed, tolerance, detail=None):
    """One check record; non-finite inputs fail instead of inventing numbers."""
    exp, obs, tol = finite_number(expected), finite_number(observed), finite_number(tolerance)
    if None in (exp, obs, tol):
        return {"name": name, "expected": exp, "observed": obs, "abs_error": None,
                "tolerance": tol, "pass": False, "detail": detail or "non-finite measurement"}
    error = abs(obs - exp)
    return {"name": name, "expected": exp, "observed": obs, "abs_error": error,
            "tolerance": tol, "pass": error <= tol, "detail": detail}


def make_flag_check(name, observed, detail=None):
    """Boolean record with the same expected, observed, error, and tolerance fields."""
    passed = bool(observed)
    return {"name": name, "expected": True, "observed": passed, "abs_error": 0.0 if passed else 1.0,
            "tolerance": 0.0, "pass": passed, "detail": detail}


def volume_tolerance(config, original_volume):
    """Protocol STEP volume tolerance: max(0.001 mm3, 1e-6 * original volume)."""
    tol = config["tolerances"]
    return max(tol["volume_abs_mm3"], tol["volume_relative"] * original_volume)


def shape_checks(name, metrics, expect_volume, expect_bbox, volume_tol, bbox_tol, source):
    """Solid, volume, and bounding-box checks for one shape against a reference."""
    observed_bbox = metrics["bbox"] or [None] * len(BBOX_KEYS)
    expected_bbox = expect_bbox or [None] * len(BBOX_KEYS)
    volume = metrics["volume"]
    checks = [
        make_check(f"{name}.solid_count", 1, metrics["solid_count"], 0.0),
        make_flag_check(f"{name}.valid_solid", metrics["valid"]),
        make_flag_check(f"{name}.volume_finite_positive", (finite_number(volume) or 0.0) > 0.0),
        make_flag_check(f"{name}.bbox_finite",
                        all(finite_number(value) is not None for value in observed_bbox)),
        make_check(f"{name}.volume_mm3", expect_volume, volume, volume_tol, f"{source} volume"),
    ]
    for key, expected, observed in zip(BBOX_KEYS, expected_bbox, observed_bbox):
        checks.append(make_check(f"{name}.bbox_{key}_mm", expected, observed, bbox_tol,
                                 f"{source} extremum"))
    return checks


def build_checks(config, measured):
    """Ordered checks for stock, cavity, final, removal, and STEP roundtrip."""
    analytic = analytic_values(config)
    bbox_tol = config["tolerances"]["bbox_extremum_mm"]
    stock, final = measured["stock"], measured["final"]
    references = [
        ("stock", analytic["stock_volume_mm3"], analytic["stock_bbox_mm"], "analytic interval"),
        ("cavity", analytic["cavity_volume_mm3"], analytic["cavity_bbox_mm"], "analytic interval"),
        ("final", analytic["final_volume_mm3"], analytic["final_bbox_mm"], "analytic interval"),
        ("imported", final["volume"], final["bbox"], "final shape before export"),
    ]
    checks = []
    for name, expect_volume, expect_bbox, source in references:
        metrics = measured.get(name)
        if metrics is None:
            checks.append(make_flag_check(f"{name}.available", False, "shape unavailable for measurement"))
            continue
        volume_tol = volume_tolerance(config, expect_volume) if finite_number(expect_volume) else None
        checks += shape_checks(name, metrics, expect_volume, expect_bbox, volume_tol, bbox_tol, source)
    removal = None
    if finite_number(stock["volume"]) is not None and finite_number(final["volume"]) is not None:
        removal = stock["volume"] - final["volume"]
    checks.append(make_check("final.removal_volume_mm3", analytic["cavity_volume_mm3"], removal,
                             volume_tolerance(config, analytic["stock_volume_mm3"]),
                             "stock volume minus final volume"))
    return checks


def check_stage(config, shapes):
    """Measure every shape and build the full check list."""
    measured = {name: None if shape is None else measure_shape(shape) for name, shape in shapes.items()}
    return measured, build_checks(config, measured)


def _stage(timings, name, func, *args, **kwargs):
    """Run a stage and record real wall and process CPU time even on failure."""
    wall_start, cpu_start = time.perf_counter(), time.process_time()
    try:
        return func(*args, **kwargs)
    finally:
        timings[name] = {"wall_s": time.perf_counter() - wall_start,
                         "cpu_s": time.process_time() - cpu_start}


def sha256_file(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def package_versions():
    """Real package versions, or the explicit string unknown when none is exposed."""
    modules = {"cadquery": ("cadquery",), "ocp": ("cadquery-ocp", "ocp"),
               "numpy": ("numpy",), "pytest": ("pytest",)}
    versions = {}
    for name, distributions in modules.items():
        module = sys.modules.get("OCP" if name == "ocp" else name)
        version = getattr(module, "__version__", None)
        for distribution in distributions:
            if version:
                break
            try:
                version = importlib.metadata.version(distribution)
            except importlib.metadata.PackageNotFoundError:
                version = None
        versions[name] = str(version) if version else "unknown"
    return versions


def step_artifact(path):
    exists = path.is_file()
    size = path.stat().st_size if exists else 0
    return {"path": str(path), "exists": exists, "size_bytes": size,
            "sha256": sha256_file(path) if size > 0 else None}


def timings_json(timings):
    """JSON-safe wall and process CPU seconds per stage, plus the total."""
    def entry(value):
        return None if value is None else {"wall_s": finite_number(value["wall_s"]),
                                           "cpu_s": finite_number(value["cpu_s"])}
    return {stage: entry(timings.get(stage)) for stage in (*STAGES, "total")}


def base_report(config, config_path, out_dir, argv):
    """Environment, provenance, and frozen-config fields for the report."""
    script_path = Path(__file__).resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "units": UNITS,
        "source_protocol": config["source_protocol"],
        "fixture": {
            "cavity_kind": config["cavity"]["kind"],
            "stock_intervals_mm": config["stock"]["intervals"],
            "cavity_intervals_mm": config["cavity"]["intervals"],
            "note": "environment smoke fixture, not the protocol rounded-pocket G0",
        },
        "output_dir": str(out_dir),
        "command": {"argv": list(argv), "executable": sys.executable,
                    "invocation": [sys.executable, str(script_path), *argv]},
        "environment": {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "versions": package_versions(),
        "config": {"path": str(config_path), "sha256": sha256_file(config_path),
                   "snapshot": json.loads(json.dumps(config))},
        "script": {"path": str(script_path), "sha256": sha256_file(script_path)},
    }


def run_smoke(config, config_path, out_dir, argv):
    """Run every stage; the returned report is always JSON-safe."""
    timings = {stage: None for stage in STAGES}
    checks, error = [], None
    step_info = {"path": str(out_dir / "final.step"), "exists": False, "size_bytes": 0, "sha256": None}
    wall_start, cpu_start = time.perf_counter(), time.process_time()
    try:
        _stage(timings, "import", import_libraries)
        stock, cavity = _stage(timings, "build", build_fixture, config)
        final = _stage(timings, "cut", cut_shapes, stock, cavity)
        step_path = out_dir / "final.step"
        _stage(timings, "export", export_step, final, step_path)
        step_info = step_artifact(step_path)
        imported = None
        if step_info["exists"] and step_info["size_bytes"] > 0:
            imported = _stage(timings, "reimport", import_step, step_path)
        shapes = {"stock": stock, "cavity": cavity, "final": final, "imported": imported}
        _, checks = _stage(timings, "check", check_stage, config, shapes)
        checks.insert(0, make_flag_check("artifact.final_step_nonempty",
                                         step_info["size_bytes"] > 0, "final.step size > 0 bytes"))
        checks.insert(0, make_flag_check("artifact.final_step_exists", step_info["exists"], step_info["path"]))
    except Exception as exc:  # recorded in the report; never converted into a PASS
        error = {"type": type(exc).__name__, "message": str(exc)}
    timings["total"] = {"wall_s": time.perf_counter() - wall_start, "cpu_s": time.process_time() - cpu_start}
    status = "ERROR" if error else ("PASS" if checks and all(check["pass"] for check in checks) else "FAIL")
    report = base_report(config, config_path, out_dir, argv)
    report.update({
        "status": status,
        "analytic": analytic_values(config),
        "checks": checks,
        "summary": {"checks_total": len(checks),
                    "checks_failed": [check["name"] for check in checks if not check["pass"]]},
        "artifacts": {"step": step_info},
        "timings": timings_json(timings),
        "error": error,
    })
    return report


def main(argv=None):
    """Run the CLI: 0 on PASS, nonzero on FAIL, ERROR, or refusal to run."""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="smoke-v1 CadQuery environment check")
    parser.add_argument("--config", required=True, help="path to the frozen smoke-v1 config")
    parser.add_argument("--out", required=True, help="new output directory for the run evidence")
    args = parser.parse_args(argv)
    config_path, out_dir = Path(args.config), Path(args.out)
    try:
        config = validate_config(json.loads(config_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, OverflowError) as exc:
        print(f"invalid config {config_path}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if out_dir.exists():
        print(f"refusing to overwrite existing output directory: {out_dir}", file=sys.stderr)
        return 2
    try:
        out_dir.mkdir(parents=True)
    except OSError as exc:
        print(f"cannot create output directory {out_dir}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    report = run_smoke(config, config_path, out_dir, argv)
    try:
        with (out_dir / "report.json").open("w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, allow_nan=False)
            handle.write("\n")
    except (OSError, ValueError) as exc:
        print(f"cannot write report: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"{report['status']}: {out_dir / 'report.json'}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
