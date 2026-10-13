'''Generate and verify G0 target design geometry for l001-Lab STEP 3.

The module builds an independent stock box and an independent rounded-rectangle
blind-pocket prism, cuts the pocket out of the stock, exports both shapes as
STEP, reimports them, and records a target-validation-v1 report. Every numeric
tolerance is a design/kernel consistency check in mm and mm^3; none of them is a
Q3 manufacturing tolerance and none of them is a G0 PASS.
'''

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cadquery as cq

from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse

from pilot.contracts import ContractError, file_checksum, semantic_hash, validate_case

GENERATOR_ID = 'pilot.targets'
GENERATOR_VERSION = '1.0'
VALIDATION_SCHEMA = 'target-validation-v1'
VALIDATION_SCOPE = 'target-geometry design/kernel consistency; not Q3 and not a G0 PASS'
MANIFEST_SCHEMA = 'target-manifest-v1'
DESIGN_SCHEMA = 'design-v1'

FROZEN_CONFIG_VERSION = 'g0-targets-v1'
FROZEN_PROTOCOL_VERSION = '1.0'
FROZEN_FAMILY_ID = 'F1'
FROZEN_MASTER_SEED = 20261012
FROZEN_SEED_DOMAIN = 'G0_TARGET'
FROZEN_SEED_RULE = "uint64_be(SHA256(f'{master_seed}|{family_id}|{case_id}|G0_TARGET')[0:8])"
FROZEN_UNITS = 'mm'
FROZEN_COORDINATES = {
    'handedness': 'right',
    'up': 'Z',
    'top_z': 0,
    'tool_axis': [0, 0, 1],
}
FROZEN_TOLERANCES = {
    'bbox_mm': 0.001,
    'volume_abs_mm3': 0.001,
    'volume_rel': 1e-6,
}
TARGET_KIND = 'rounded_rect_blind_pocket'

FROZEN_CASES: Tuple[Dict[str, Any], ...] = (
    {
        'case_id': 'S01',
        'family_id': 'F1',
        'ancestor_id': None,
        'stock': {'L': 60, 'W': 50, 'H': 16},
        'target': {'kind': TARGET_KIND, 'a': 30, 'b': 24, 'R': 3, 'd': 6},
    },
    {
        'case_id': 'S02',
        'family_id': 'F1',
        'ancestor_id': None,
        'stock': {'L': 80, 'W': 45, 'H': 16},
        'target': {'kind': TARGET_KIND, 'a': 54, 'b': 14, 'R': 3, 'd': 5},
    },
    {
        'case_id': 'S03',
        'family_id': 'F1',
        'ancestor_id': None,
        'stock': {'L': 60, 'W': 55, 'H': 20},
        'target': {'kind': TARGET_KIND, 'a': 36, 'b': 30, 'R': 4, 'd': 12},
    },
    {
        'case_id': 'S04',
        'family_id': 'F1',
        'ancestor_id': None,
        'stock': {'L': 70, 'W': 55, 'H': 16},
        'target': {'kind': TARGET_KIND, 'a': 46, 'b': 32, 'R': 2, 'd': 4},
    },
    {
        'case_id': 'S05',
        'family_id': 'F1',
        'ancestor_id': None,
        'stock': {'L': 60, 'W': 50, 'H': 12},
        'target': {'kind': TARGET_KIND, 'a': 48, 'b': 36, 'R': 3, 'd': 3},
    },
    {
        'case_id': 'S06',
        'family_id': 'F1',
        'ancestor_id': None,
        'stock': {'L': 55, 'W': 50, 'H': 20},
        'target': {'kind': TARGET_KIND, 'a': 24, 'b': 20, 'R': 4, 'd': 10},
    },
)

_EPS = 1e-9
_CASE_SPEC_KEYS = {'case_id', 'family_id', 'ancestor_id', 'stock', 'target'}
_SAFE_CASE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


class TargetError(RuntimeError):
    '''Raised when target generation cannot proceed or cannot be verified.

    ``code`` is the reason code reported for the run: ``ERROR_INPUT`` and
    ``ERROR`` for operational or input failures and ``UNKNOWN_UNSUPPORTED``
    for unsupported inputs, which the generator reports as UNKNOWN.
    '''

    def __init__(self, message: str, *, code: str = 'ERROR') -> None:
        super().__init__(message)
        self.code = code


def _validate_master_seed(value: Any) -> int:
    '''Return a validated uint64 master seed, rejecting bool and non-int values.'''
    if isinstance(value, bool) or not isinstance(value, int):
        raise TargetError('master_seed must be a uint64 integer', code='ERROR_INPUT')
    if value < 0 or value >= 2 ** 64:
        raise TargetError('master_seed must be a uint64 integer', code='ERROR_INPUT')
    return int(value)


def _semantic_equal(left: Any, right: Any) -> bool:
    '''Compare two JSON values by semantic hash, keeping bool distinct from 0/1.'''
    try:
        return semantic_hash(left) == semantic_hash(right)
    except Exception:  # a value semantic_hash cannot canonicalize is not the frozen value
        return False


def _status_for_code(code: Optional[str]) -> str:
    '''Map an internal reason code to the externally reported status token.'''
    if code == 'UNKNOWN_UNSUPPORTED':
        return 'UNKNOWN'
    if code == 'FAIL':
        return 'FAIL'
    return 'ERROR'


def _safe_case_id(value: Any) -> str:
    '''Validate a case id as one safe path component before any filesystem use.'''
    if not isinstance(value, str) or not value:
        raise TargetError('case spec requires a non-empty string case_id', code='ERROR_INPUT')
    if not _SAFE_CASE_ID.fullmatch(value):
        raise TargetError(
            'case_id must be a single safe path component without separators, traversal, drive or control characters',
            code='ERROR_INPUT',
        )
    return value


# ---------------------------------------------------------------------------
# frozen seed rule
# ---------------------------------------------------------------------------


def derive_seed(master_seed: int, family_id: str, case_id: str) -> int:
    '''Derive the frozen uint64 child seed for one case.

    Rule: take the first 8 bytes of SHA256 of the pipe-joined text
    master_seed, family_id, case_id and the frozen domain G0_TARGET, and read
    them as a big-endian unsigned 64-bit integer.
    '''
    _validate_master_seed(master_seed)
    if not isinstance(family_id, str) or not family_id:
        raise TargetError('family_id must be a non-empty string', code='ERROR_INPUT')
    if not isinstance(case_id, str) or not case_id:
        raise TargetError('case_id must be a non-empty string', code='ERROR_INPUT')
    payload = '{0}|{1}|{2}|{3}'.format(int(master_seed), family_id, case_id, FROZEN_SEED_DOMAIN)
    digest = hashlib.sha256(payload.encode('utf-8')).digest()
    return int.from_bytes(digest[:8], 'big')


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def _canonical_case_row(row: Any) -> Dict[str, Any]:
    if not isinstance(row, dict):
        raise TargetError('each case row must be a JSON object', code='ERROR_INPUT')
    allowed = {'case_id', 'family_id', 'ancestor_id', 'stock', 'target'}
    extra = set(row) - allowed
    if extra:
        raise TargetError('unknown case row keys: {0}'.format(sorted(extra)), code='ERROR_INPUT')
    try:
        stock = row['stock']
        target = row['target']
    except KeyError as exc:
        raise TargetError('case row is missing a required field: {0}'.format(exc), code='ERROR_INPUT') from exc
    if not isinstance(stock, dict) or not isinstance(target, dict):
        raise TargetError('case row stock and target must be JSON objects', code='ERROR_INPUT')
    stock_extra = set(stock) - {'L', 'W', 'H'}
    if stock_extra:
        raise TargetError('unknown stock keys: {0}'.format(sorted(stock_extra)), code='ERROR_INPUT')
    target_extra = set(target) - {'kind', 'a', 'b', 'R', 'd'}
    if target_extra:
        raise TargetError('unknown target keys: {0}'.format(sorted(target_extra)), code='ERROR_INPUT')
    try:
        return {
            'case_id': row['case_id'],
            'family_id': row['family_id'],
            'ancestor_id': row.get('ancestor_id'),
            'stock': {'L': stock['L'], 'W': stock['W'], 'H': stock['H']},
            'target': {
                'kind': target['kind'],
                'a': target['a'],
                'b': target['b'],
                'R': target['R'],
                'd': target['d'],
            },
        }
    except KeyError as exc:
        raise TargetError('case row is missing a required field: {0}'.format(exc), code='ERROR_INPUT') from exc


def _validate_case_row(row: Dict[str, Any]) -> None:
    if row['target']['kind'] != TARGET_KIND:
        raise TargetError('unsupported target kind: {0!r}'.format(row['target']['kind']), code='UNKNOWN_UNSUPPORTED')
    values = (
        row['stock']['L'], row['stock']['W'], row['stock']['H'],
        row['target']['a'], row['target']['b'], row['target']['R'], row['target']['d'],
    )
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TargetError('case dimensions must be numbers', code='ERROR_INPUT')
        if not math.isfinite(float(value)) or float(value) <= 0.0:
            raise TargetError('case dimensions must be positive and finite', code='ERROR_INPUT')


def _validate_config(data: Any, *, enforce_frozen: bool) -> None:
    if not isinstance(data, dict):
        raise TargetError('config must be a JSON object', code='ERROR_INPUT')
    required = {
        'config_version', 'protocol_version', 'family_id', 'master_seed',
        'units', 'coordinates', 'seed_rule', 'tolerances', 'cases',
    }
    missing = required - set(data)
    if missing:
        raise TargetError('config is missing required keys: {0}'.format(sorted(missing)), code='ERROR_INPUT')
    extra = set(data) - required
    if extra:
        raise TargetError('unknown config keys: {0}'.format(sorted(extra)), code='ERROR_INPUT')
    version = data.get('config_version')
    if not isinstance(version, str) or not version:
        raise TargetError('config_version must be a non-empty string', code='ERROR_INPUT')
    _validate_master_seed(data.get('master_seed'))
    if enforce_frozen:
        if version != FROZEN_CONFIG_VERSION:
            raise TargetError('unsupported config_version: {0!r}'.format(version), code='UNKNOWN_UNSUPPORTED')
        frozen_fields = (
            ('protocol_version', FROZEN_PROTOCOL_VERSION),
            ('family_id', FROZEN_FAMILY_ID),
            ('master_seed', FROZEN_MASTER_SEED),
            ('units', FROZEN_UNITS),
            ('coordinates', FROZEN_COORDINATES),
            ('seed_rule', FROZEN_SEED_RULE),
            ('tolerances', FROZEN_TOLERANCES),
        )
        for key, expected in frozen_fields:
            if data.get(key) != expected:
                raise TargetError(
                    'frozen config field {0!r} was modified inside {1}'.format(key, FROZEN_CONFIG_VERSION),
                    code='ERROR_INPUT',
                )
    else:
        if data.get('protocol_version') != FROZEN_PROTOCOL_VERSION:
            raise TargetError(
                'unsupported protocol_version: {0!r}'.format(data.get('protocol_version')),
                code='UNKNOWN_UNSUPPORTED',
            )
        if data.get('units') != FROZEN_UNITS:
            raise TargetError('config units must be {0!r}'.format(FROZEN_UNITS), code='ERROR_INPUT')
        if data.get('family_id') != FROZEN_FAMILY_ID:
            raise TargetError('config family_id must be {0!r}'.format(FROZEN_FAMILY_ID), code='ERROR_INPUT')
        if data.get('seed_rule') != FROZEN_SEED_RULE:
            raise TargetError('config must use the frozen seed rule', code='ERROR_INPUT')
        if not _semantic_equal(data.get('coordinates'), FROZEN_COORDINATES):
            raise TargetError('config must use the frozen coordinates', code='ERROR_INPUT')
        if data.get('tolerances') != FROZEN_TOLERANCES:
            raise TargetError('config must use the frozen tolerances', code='ERROR_INPUT')
    cases = data.get('cases')
    if not isinstance(cases, list) or not cases:
        raise TargetError('config must contain a non-empty cases list', code='ERROR_INPUT')
    normalized = [_canonical_case_row(row) for row in cases]
    if enforce_frozen:
        expected_rows = [_canonical_case_row(row) for row in FROZEN_CASES]
        if normalized != expected_rows:
            raise TargetError(
                'the six frozen case rows were modified inside {0}'.format(FROZEN_CONFIG_VERSION),
                code='ERROR_INPUT',
            )
    case_ids = [str(row['case_id']) for row in normalized]
    if len(case_ids) != len(set(case_ids)):
        raise TargetError('config contains duplicate case ids', code='ERROR_INPUT')
    for row in normalized:
        _validate_case_row(row)
        if row['family_id'] != data.get('family_id'):
            raise TargetError(
                'case {0} family_id does not match config family_id'.format(row['case_id']),
                code='ERROR_INPUT',
            )


def load_config(path: Any) -> Dict[str, Any]:
    '''Read and validate a target config; frozen v1 fields cannot be changed.'''
    config_path = Path(path)
    if not config_path.is_file():
        raise TargetError('config file not found: {0}'.format(config_path.name), code='ERROR_INPUT')
    try:
        data = json.loads(config_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise TargetError('config is not readable JSON: {0}'.format(exc), code='ERROR_INPUT') from exc
    _validate_config(data, enforce_frozen=True)
    return data


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


def _positive(value: Any, name: str) -> float:
    if value is None:
        raise TargetError('{0} is required'.format(name), code='ERROR_INPUT')
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TargetError('{0} must be numeric'.format(name), code='ERROR_INPUT')
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TargetError('{0} must be numeric'.format(name), code='ERROR_INPUT') from exc
    if not math.isfinite(number) or number <= 0.0:
        raise TargetError('{0} must be positive and finite'.format(name), code='ERROR_INPUT')
    return number


def _cast(topo: Any) -> Any:
    '''Wrap a TopoDS_Shape in the most specific CadQuery Shape subclass available.'''
    cast = getattr(cq.Shape, 'cast', None)
    if callable(cast):
        return cast(topo)
    return cq.Shape(topo)


def _clean_shape(shape: Any) -> Any:
    '''Merge coplanar faces on a built shape so one trimmed face stays one face.'''
    clean = getattr(shape, 'clean', None)
    if not callable(clean):
        return shape
    try:
        cleaned = clean()
    except Exception as exc:  # a kernel clean failure is an ERROR, never a silent pass
        raise TargetError('shape clean failed in the geometry kernel', code='ERROR') from exc
    if cleaned is None:
        return shape
    return cleaned if hasattr(cleaned, 'wrapped') else _cast(cleaned)


def _fuse_solids(solids: List[Any]) -> Any:
    if not solids:
        raise TargetError('pocket union needs at least one solid')
    accumulated = solids[0].wrapped
    for solid in solids[1:]:
        operation = BRepAlgoAPI_Fuse(accumulated, solid.wrapped)
        operation.Build()
        if not operation.IsDone():
            raise TargetError('pocket union failed in the geometry kernel')
        accumulated = operation.Shape()
    return _clean_shape(_cast(accumulated))


def _cut(stock: Any, tool: Any) -> Any:
    operation = BRepAlgoAPI_Cut(stock.wrapped, tool.wrapped)
    operation.Build()
    if not operation.IsDone():
        raise TargetError('stock minus pocket Boolean failed in the geometry kernel')
    return _clean_shape(_cast(operation.Shape()))


def _build_pocket(a: float, b: float, radius: float, depth: float, length: float, width: float) -> Any:
    '''Union of positive central rectangles and corner cylinders at design centers.

    Coincident corner centers are deduplicated and zero-width cores are skipped
    so that the contract radius bound R = min(a, b) / 2 stays valid (capsule or
    full circle).
    '''
    center_x = length / 2.0
    center_y = width / 2.0
    top = -depth
    parts: List[Any] = []
    core_x = a - 2.0 * radius
    core_y = b - 2.0 * radius
    direction = cq.Vector(0.0, 0.0, 1.0)
    if core_x > 0.0:
        parts.append(
            cq.Solid.makeBox(
                core_x, b, depth,
                cq.Vector(center_x - core_x / 2.0, center_y - b / 2.0, top),
                direction,
            )
        )
    if core_y > 0.0:
        parts.append(
            cq.Solid.makeBox(
                a, core_y, depth,
                cq.Vector(center_x - a / 2.0, center_y - core_y / 2.0, top),
                direction,
            )
        )
    seen = set()
    for sign_x in (-1.0, 1.0):
        for sign_y in (-1.0, 1.0):
            point_x = center_x + sign_x * (a / 2.0 - radius)
            point_y = center_y + sign_y * (b / 2.0 - radius)
            key = (point_x, point_y)
            if key in seen:
                continue
            seen.add(key)
            parts.append(
                cq.Solid.makeCylinder(
                    radius, depth,
                    cq.Vector(point_x, point_y, top),
                    direction,
                )
            )
    if not parts:
        raise TargetError('degenerate pocket prism: no positive core and no corner cylinder')
    if len(parts) == 1:
        return parts[0]
    return _fuse_solids(parts)


def build_design(spec: Dict[str, Any]) -> Dict[str, Any]:
    '''Build stock, design pocket and target solids from design parameters only.

    The result depends purely on the dimensions in spec; no candidate, tool,
    toolpath or sweep input is accepted. The root is either a two-field design
    {stock, target} or a five-field case spec {case_id, family_id, ancestor_id,
    stock, target}; unknown fields and non-numeric dimensions are rejected
    rather than ignored.
    '''
    if not isinstance(spec, dict):
        raise TargetError('design spec must be a JSON object', code='ERROR_INPUT')
    extra = set(spec) - _CASE_SPEC_KEYS
    if extra:
        raise TargetError('unknown design spec keys: {0}'.format(sorted(extra)), code='ERROR_INPUT')
    if 'stock' not in spec or 'target' not in spec:
        raise TargetError('design spec requires stock and target', code='ERROR_INPUT')
    stock_spec = spec['stock']
    target_spec = spec['target']
    if not isinstance(stock_spec, dict) or not isinstance(target_spec, dict):
        raise TargetError('design spec stock and target must be JSON objects', code='ERROR_INPUT')
    stock_extra = set(stock_spec) - {'L', 'W', 'H'}
    if stock_extra:
        raise TargetError('unknown stock keys: {0}'.format(sorted(stock_extra)), code='ERROR_INPUT')
    target_extra = set(target_spec) - {'kind', 'a', 'b', 'R', 'd'}
    if target_extra:
        raise TargetError('unknown target keys: {0}'.format(sorted(target_extra)), code='ERROR_INPUT')
    length = _positive(stock_spec.get('L'), 'stock.L')
    width = _positive(stock_spec.get('W'), 'stock.W')
    height = _positive(stock_spec.get('H'), 'stock.H')
    a = _positive(target_spec.get('a'), 'target.a')
    b = _positive(target_spec.get('b'), 'target.b')
    radius = _positive(target_spec.get('R'), 'target.R')
    depth = _positive(target_spec.get('d'), 'target.d')
    kind = target_spec.get('kind', TARGET_KIND)
    if kind != TARGET_KIND:
        raise TargetError('unsupported target kind: {0!r}'.format(kind), code='UNKNOWN_UNSUPPORTED')
    if a >= length or b >= width:
        raise TargetError('target pocket must be strictly inside the stock footprint', code='ERROR_INPUT')
    if depth >= height:
        raise TargetError('target pocket depth must be smaller than the stock height', code='ERROR_INPUT')
    if radius > min(a, b) / 2.0:
        raise TargetError('target.R exceeds min(a, b) / 2', code='ERROR_INPUT')
    stock = cq.Solid.makeBox(
        length, width, height,
        cq.Vector(0.0, 0.0, -height),
        cq.Vector(0.0, 0.0, 1.0),
    )
    pocket = _build_pocket(a, b, radius, depth, length, width)
    target = _cut(stock, pocket)
    return {
        'design': {
            'stock': {'L': length, 'W': width, 'H': height},
            'target': {'kind': kind, 'a': a, 'b': b, 'R': radius, 'd': depth},
        },
        'stock': stock,
        'pocket': pocket,
        'target': target,
    }


# ---------------------------------------------------------------------------
# measurements
# ---------------------------------------------------------------------------


def analytic_pocket_volume(a: float, b: float, radius: float, depth: float) -> float:
    '''Analytic rounded-rectangle blind pocket volume: (a*b - (4 - pi) * R^2) * d.'''
    return (a * b - (4.0 - math.pi) * radius * radius) * depth


def volume_tolerance(original_volume: float) -> float:
    '''Frozen volume tolerance: max(0.001 mm^3, 1e-6 * original volume).'''
    return max(
        FROZEN_TOLERANCES['volume_abs_mm3'],
        FROZEN_TOLERANCES['volume_rel'] * abs(float(original_volume)),
    )


def _union_bbox(boxes: List[Any]) -> Optional[Dict[str, float]]:
    if not boxes:
        return None
    return {
        'xmin': min(box.xmin for box in boxes),
        'xmax': max(box.xmax for box in boxes),
        'ymin': min(box.ymin for box in boxes),
        'ymax': max(box.ymax for box in boxes),
        'zmin': min(box.zmin for box in boxes),
        'zmax': max(box.zmax for box in boxes),
    }


def measure_roots(roots: List[Any]) -> Dict[str, Any]:
    '''Measure solid count, validity, volume, bounding box and faces of shape roots.'''
    solids: List[Any] = []
    for root in roots:
        solids.extend(root.Solids())
    faces: List[Any] = []
    for solid in solids:
        faces.extend(solid.Faces())
    volume = 0.0
    finite = True
    valid = bool(solids)
    boxes: List[Any] = []
    for solid in solids:
        try:
            solid_volume = float(solid.Volume())
        except Exception:  # kernel accessor failure is a measurement failure, not a crash
            solid_volume = float('nan')
        if not math.isfinite(solid_volume):
            finite = False
        volume += solid_volume
        try:
            if not solid.isValid():
                valid = False
        except Exception:  # a solid that cannot report validity is not valid for this run
            valid = False
        try:
            boxes.append(solid.BoundingBox())
        except Exception:  # a solid without a bounding box cannot be checked
            finite = False
    return {
        'solid_count': len(solids),
        'valid': valid,
        'finite': finite,
        'volume': volume,
        'bbox': _union_bbox(boxes),
        'faces': faces,
    }


def floor_faces(faces: List[Any], z_bottom: float, z_top: float, planarity_tol: float = 1e-6) -> Dict[str, Any]:
    '''Aggregate horizontal planar faces strictly between the stock bottom and top.

    Every face must expose its geometry: an accessor failure is raised as a
    kernel ERROR instead of being skipped, and the per-face heights are kept so
    callers can require every floor face at the same Z level.
    '''
    heights: List[float] = []
    xs: List[float] = []
    ys: List[float] = []
    count = 0
    for face in faces:
        try:
            if face.geomType() != 'PLANE':
                continue
            box = face.BoundingBox()
        except Exception as exc:  # an unreadable face is an ERROR, never a silent skip
            raise TargetError('floor face geometry accessor failed: {0}'.format(exc), code='ERROR') from exc
        if abs(box.zmax - box.zmin) > planarity_tol:
            continue
        z = 0.5 * (box.zmax + box.zmin)
        if not (z_bottom + 1e-9 < z < z_top - 1e-9):
            continue
        count += 1
        heights.append(z)
        xs.extend([box.xmin, box.xmax])
        ys.extend([box.ymin, box.ymax])
    if count == 0:
        return {
            'count': 0, 'z': None, 'z_min': None, 'z_max': None,
            'xmin': None, 'xmax': None, 'ymin': None, 'ymax': None,
        }
    return {
        'count': count,
        'z': sum(heights) / len(heights),
        'z_min': min(heights),
        'z_max': max(heights),
        'xmin': min(xs),
        'xmax': max(xs),
        'ymin': min(ys),
        'ymax': max(ys),
    }


def cylinder_faces(faces: List[Any]) -> List[Dict[str, Any]]:
    '''Read real cylindrical face radii and axes from the OCC surface adaptor.'''
    found: List[Dict[str, Any]] = []
    for face in faces:
        try:
            if face.geomType() != 'CYLINDER':
                continue
            surface = BRepAdaptor_Surface(face.wrapped)
            cylinder = surface.Cylinder()
            radius = float(cylinder.Radius())
            direction = cylinder.Axis().Direction()
            components = (float(direction.X()), float(direction.Y()), float(direction.Z()))
        except Exception as exc:  # an unreadable face is an ERROR, never a silent skip
            raise TargetError('cylinder face geometry accessor failed: {0}'.format(exc), code='ERROR') from exc
        found.append({
            'radius': radius,
            'axis': list(components),
            'vertical': abs(abs(components[2]) - 1.0) <= 1e-6,
        })
    return found


# ---------------------------------------------------------------------------
# report plumbing
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else str(value)
    item_method = getattr(value, 'item', None)
    if callable(item_method):
        try:
            return _jsonable(item_method())
        except Exception:  # a scalar that cannot convert falls back to its text form
            return str(value)
    return str(value)


def _num(value: Any) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else 'non_finite'


def _close(observed: Any, expected: Any, tolerance: float) -> bool:
    try:
        return abs(float(observed) - float(expected)) <= tolerance
    except (TypeError, ValueError):
        return False


def _bbox_close(observed: Optional[Dict[str, float]], expected: Dict[str, float], tolerance: float) -> bool:
    if observed is None:
        return False
    return all(_close(observed[key], expected[key], tolerance) for key in expected)


def _check(
    name: str,
    scope: str,
    observed: Any,
    expected: Any,
    ok: bool,
    errors: Optional[List[str]] = None,
    tolerances: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        'name': name,
        'scope': scope,
        'observed': _jsonable(observed),
        'expected': _jsonable(expected),
        'errors': list(errors or []),
        'tolerances': _jsonable(tolerances or {}),
        'pass': bool(ok),
    }


def _write_json(path: Path, payload: Any) -> None:
    text = json.dumps(_jsonable(payload), indent=2, sort_keys=True, allow_nan=False)
    path.write_text(text + '\n', encoding='utf-8')


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def design_checks(design: Dict[str, Any], tolerances: Dict[str, float]) -> List[Dict[str, Any]]:
    '''Analytic design-parameter checks only. Scope: design.'''
    length = float(design['stock']['L'])
    width = float(design['stock']['W'])
    height = float(design['stock']['H'])
    a = float(design['target']['a'])
    b = float(design['target']['b'])
    radius = float(design['target']['R'])
    depth = float(design['target']['d'])
    bbox_tol = float(tolerances['bbox_mm'])
    checks: List[Dict[str, Any]] = []

    dims_ok = all(math.isfinite(v) and v > 0.0 for v in (length, width, height, a, b, radius, depth))
    checks.append(_check(
        'design.dimensions_positive', 'design',
        {'L': length, 'W': width, 'H': height, 'a': a, 'b': b, 'R': radius, 'd': depth},
        'all dimensions > 0',
        dims_ok,
        [] if dims_ok else ['one or more design dimensions are not positive finite numbers'],
        {'bbox_mm': bbox_tol},
    ))

    radius_ok = radius <= min(a, b) / 2.0
    checks.append(_check(
        'design.radius_contract', 'design',
        {'R': radius, 'min_half': min(a, b) / 2.0},
        'R <= min(a, b) / 2',
        radius_ok,
        [] if radius_ok else ['R exceeds min(a, b) / 2'],
        {'bbox_mm': bbox_tol},
    ))

    fit_ok = a < length and b < width and 0.0 < depth < height
    checks.append(_check(
        'design.fit_inside_stock', 'design',
        {'a': a, 'L': length, 'b': b, 'W': width, 'd': depth, 'H': height},
        'a < L, b < W, 0 < d < H',
        fit_ok,
        [] if fit_ok else ['pocket does not fit strictly inside the stock'],
        {'bbox_mm': bbox_tol},
    ))

    pocket_volume = analytic_pocket_volume(a, b, radius, depth)
    pocket_ok = math.isfinite(pocket_volume) and pocket_volume > 0.0
    checks.append(_check(
        'design.analytic_pocket_volume', 'design',
        {'pocket_volume_mm3': pocket_volume},
        'positive finite analytic pocket volume',
        pocket_ok,
        [] if pocket_ok else ['analytic pocket volume is not positive finite'],
        {'volume_abs_mm3': tolerances['volume_abs_mm3'], 'volume_rel': tolerances['volume_rel']},
    ))

    stock_volume = length * width * height
    target_volume = stock_volume - pocket_volume
    target_ok = math.isfinite(target_volume) and target_volume > 0.0
    checks.append(_check(
        'design.analytic_target_volume', 'design',
        {'target_volume_mm3': target_volume, 'stock_volume_mm3': stock_volume},
        'stock volume minus analytic pocket volume is positive finite',
        target_ok,
        [] if target_ok else ['analytic target volume is not positive finite'],
        {'volume_abs_mm3': tolerances['volume_abs_mm3'], 'volume_rel': tolerances['volume_rel']},
    ))
    return checks


def stock_checks(label: str, design: Dict[str, Any], roots: List[Any], tolerances: Dict[str, float]) -> List[Dict[str, Any]]:
    '''Kernel checks of the stock solid. Scope: kernel.'''
    length = float(design['stock']['L'])
    width = float(design['stock']['W'])
    height = float(design['stock']['H'])
    bbox_tol = float(tolerances['bbox_mm'])
    measured = measure_roots(roots)
    checks: List[Dict[str, Any]] = []

    solid_ok = (
        measured['solid_count'] == 1
        and measured['valid']
        and measured['finite']
        and measured['volume'] > 0.0
    )
    checks.append(_check(
        '{0}.stock_solid'.format(label), 'kernel',
        {'solid_count': measured['solid_count'], 'valid': measured['valid'], 'finite': measured['finite']},
        {'solid_count': 1, 'valid': True, 'finite': True},
        solid_ok,
        [] if solid_ok else ['stock is not exactly one valid finite solid'],
        {'bbox_mm': bbox_tol},
    ))

    expected_bbox = {'xmin': 0.0, 'xmax': length, 'ymin': 0.0, 'ymax': width, 'zmin': -height, 'zmax': 0.0}
    bbox_ok = _bbox_close(measured['bbox'], expected_bbox, bbox_tol)
    checks.append(_check(
        '{0}.stock_outer_bbox'.format(label), 'kernel',
        measured['bbox'], expected_bbox, bbox_ok,
        [] if bbox_ok else ['stock bounding box differs from [0,L]x[0,W]x[-H,0]'],
        {'bbox_mm': bbox_tol},
    ))

    expected_volume = length * width * height
    tolerance = volume_tolerance(expected_volume)
    volume_ok = measured['finite'] and _close(measured['volume'], expected_volume, tolerance)
    checks.append(_check(
        '{0}.stock_volume'.format(label), 'kernel',
        {
            'stock_volume_mm3': _num(measured['volume']),
            'analytic_mm3': expected_volume,
            'abs_error_mm3': abs(measured['volume'] - expected_volume) if measured['finite'] else None,
        },
        {'abs_error_mm3': '<= {0}'.format(tolerance)},
        volume_ok,
        [] if volume_ok else ['stock volume differs from L*W*H beyond tolerance'],
        {'volume_abs_mm3': tolerances['volume_abs_mm3'], 'volume_rel': tolerances['volume_rel']},
    ))
    return checks


def pocket_checks(label: str, design: Dict[str, Any], roots: List[Any], tolerances: Dict[str, float]) -> List[Dict[str, Any]]:
    '''Kernel checks of the independent design pocket prism. Scope: kernel.'''
    length = float(design['stock']['L'])
    width = float(design['stock']['W'])
    a = float(design['target']['a'])
    b = float(design['target']['b'])
    radius = float(design['target']['R'])
    depth = float(design['target']['d'])
    bbox_tol = float(tolerances['bbox_mm'])
    measured = measure_roots(roots)
    checks: List[Dict[str, Any]] = []

    solid_ok = (
        measured['solid_count'] == 1
        and measured['valid']
        and measured['finite']
        and measured['volume'] > 0.0
    )
    checks.append(_check(
        '{0}.pocket_solid'.format(label), 'kernel',
        {'solid_count': measured['solid_count'], 'valid': measured['valid'], 'finite': measured['finite']},
        {'solid_count': 1, 'valid': True, 'finite': True},
        solid_ok,
        [] if solid_ok else ['design pocket prism is not exactly one valid finite solid'],
        {'bbox_mm': bbox_tol},
    ))

    expected_bbox = {
        'xmin': length / 2.0 - a / 2.0,
        'xmax': length / 2.0 + a / 2.0,
        'ymin': width / 2.0 - b / 2.0,
        'ymax': width / 2.0 + b / 2.0,
        'zmin': -depth,
        'zmax': 0.0,
    }
    bbox_ok = _bbox_close(measured['bbox'], expected_bbox, bbox_tol)
    checks.append(_check(
        '{0}.pocket_bbox'.format(label), 'kernel',
        measured['bbox'], expected_bbox, bbox_ok,
        [] if bbox_ok else ['design pocket bounding box differs from the analytic footprint'],
        {'bbox_mm': bbox_tol},
    ))

    expected_volume = analytic_pocket_volume(a, b, radius, depth)
    tolerance = volume_tolerance(expected_volume)
    volume_ok = measured['finite'] and _close(measured['volume'], expected_volume, tolerance)
    checks.append(_check(
        '{0}.pocket_volume'.format(label), 'kernel',
        {
            'pocket_volume_mm3': _num(measured['volume']),
            'analytic_mm3': expected_volume,
            'abs_error_mm3': abs(measured['volume'] - expected_volume) if measured['finite'] else None,
        },
        {'abs_error_mm3': '<= {0}'.format(tolerance)},
        volume_ok,
        [] if volume_ok else ['design pocket volume differs from the analytic volume beyond tolerance'],
        {'volume_abs_mm3': tolerances['volume_abs_mm3'], 'volume_rel': tolerances['volume_rel']},
    ))
    return checks


def target_checks(label: str, design: Dict[str, Any], roots: List[Any], tolerances: Dict[str, float]) -> List[Dict[str, Any]]:
    '''Kernel checks of the cut target: solid, bbox, volume, floor and real radii.'''
    length = float(design['stock']['L'])
    width = float(design['stock']['W'])
    height = float(design['stock']['H'])
    a = float(design['target']['a'])
    b = float(design['target']['b'])
    radius = float(design['target']['R'])
    depth = float(design['target']['d'])
    bbox_tol = float(tolerances['bbox_mm'])
    measured = measure_roots(roots)
    checks: List[Dict[str, Any]] = []

    solid_ok = (
        measured['solid_count'] == 1
        and measured['valid']
        and measured['finite']
        and measured['volume'] > 0.0
    )
    checks.append(_check(
        '{0}.target_solid'.format(label), 'kernel',
        {'solid_count': measured['solid_count'], 'valid': measured['valid'], 'finite': measured['finite']},
        {'solid_count': 1, 'valid': True, 'finite': True},
        solid_ok,
        [] if solid_ok else ['target is not exactly one valid finite solid'],
        {'bbox_mm': bbox_tol},
    ))

    expected_bbox = {'xmin': 0.0, 'xmax': length, 'ymin': 0.0, 'ymax': width, 'zmin': -height, 'zmax': 0.0}
    bbox_ok = _bbox_close(measured['bbox'], expected_bbox, bbox_tol)
    checks.append(_check(
        '{0}.target_outer_bbox'.format(label), 'kernel',
        measured['bbox'], expected_bbox, bbox_ok,
        [] if bbox_ok else ['target outer bounding box differs from the stock bounding box'],
        {'bbox_mm': bbox_tol},
    ))

    expected_volume = length * width * height - analytic_pocket_volume(a, b, radius, depth)
    tolerance = volume_tolerance(expected_volume)
    volume_ok = measured['finite'] and _close(measured['volume'], expected_volume, tolerance)
    checks.append(_check(
        '{0}.target_volume'.format(label), 'kernel',
        {
            'target_volume_mm3': _num(measured['volume']),
            'analytic_mm3': expected_volume,
            'abs_error_mm3': abs(measured['volume'] - expected_volume) if measured['finite'] else None,
        },
        {'abs_error_mm3': '<= {0}'.format(tolerance)},
        volume_ok,
        [] if volume_ok else ['target volume differs from stock minus analytic pocket beyond tolerance'],
        {'volume_abs_mm3': tolerances['volume_abs_mm3'], 'volume_rel': tolerances['volume_rel']},
    ))

    floor = floor_faces(measured['faces'], -height, 0.0)
    floor_z = floor['z']
    floor_ok = (
        floor['count'] > 0
        and floor['z_min'] is not None
        and floor['z_max'] is not None
        and _close(floor['z_min'], -depth, bbox_tol)
        and _close(floor['z_max'], -depth, bbox_tol)
    )
    checks.append(_check(
        '{0}.target_floor_plane'.format(label), 'kernel',
        {
            'floor_face_count': floor['count'],
            'floor_z_mm': _num(floor_z) if floor_z is not None else None,
            'floor_z_min_mm': _num(floor['z_min']) if floor['z_min'] is not None else None,
            'floor_z_max_mm': _num(floor['z_max']) if floor['z_max'] is not None else None,
        },
        {'floor_face_count_min': 1, 'floor_z_mm': -depth, 'floor_z_min_mm': -depth, 'floor_z_max_mm': -depth},
        floor_ok,
        [] if floor_ok else ['not every horizontal planar floor face sits at Z = -d'],
        {'bbox_mm': bbox_tol},
    ))

    if floor['count'] > 0:
        measured_a = floor['xmax'] - floor['xmin']
        measured_b = floor['ymax'] - floor['ymin']
        center_x = 0.5 * (floor['xmin'] + floor['xmax'])
        center_y = 0.5 * (floor['ymin'] + floor['ymax'])
        footprint_ok = (
            _close(measured_a, a, bbox_tol)
            and _close(measured_b, b, bbox_tol)
            and _close(center_x, length / 2.0, bbox_tol)
            and _close(center_y, width / 2.0, bbox_tol)
        )
        checks.append(_check(
            '{0}.target_floor_footprint'.format(label), 'kernel',
            {'a_mm': measured_a, 'b_mm': measured_b, 'center_x_mm': center_x, 'center_y_mm': center_y},
            {'a_mm': a, 'b_mm': b, 'center_x_mm': length / 2.0, 'center_y_mm': width / 2.0},
            footprint_ok,
            [] if footprint_ok else ['actual floor footprint dimensions or centering differ from design'],
            {'bbox_mm': bbox_tol},
        ))
        bottom = floor_z + height
        left = floor['xmin']
        right = length - floor['xmax']
        front = floor['ymin']
        back = width - floor['ymax']
        expected_side = (length - a) / 2.0
        expected_front = (width - b) / 2.0
        wall_ok = (
            _close(bottom, height - depth, bbox_tol) and bottom > 0.0
            and _close(left, expected_side, bbox_tol) and left > 0.0
            and _close(right, expected_side, bbox_tol) and right > 0.0
            and _close(front, expected_front, bbox_tol) and front > 0.0
            and _close(back, expected_front, bbox_tol) and back > 0.0
        )
        checks.append(_check(
            '{0}.target_wall_bottom_thickness'.format(label), 'kernel',
            {'bottom_mm': bottom, 'left_mm': left, 'right_mm': right, 'front_mm': front, 'back_mm': back},
            {
                'bottom_mm': height - depth,
                'left_mm': expected_side,
                'right_mm': expected_side,
                'front_mm': expected_front,
                'back_mm': expected_front,
            },
            wall_ok,
            [] if wall_ok else ['actual bottom or side wall thickness is non-positive or differs from design'],
            {'bbox_mm': bbox_tol},
        ))
    else:
        checks.append(_check(
            '{0}.target_floor_footprint'.format(label), 'kernel',
            {'floor_face_count': 0}, {'floor_face_count_min': 1}, False,
            ['floor face was not identified, so footprint and wall thickness cannot be measured'],
            {'bbox_mm': bbox_tol},
        ))
        checks.append(_check(
            '{0}.target_wall_bottom_thickness'.format(label), 'kernel',
            {'floor_face_count': 0}, {'floor_face_count_min': 1}, False,
            ['floor face was not identified, so bottom and side wall thickness cannot be measured'],
            {'bbox_mm': bbox_tol},
        ))

    cylinders = cylinder_faces(measured['faces'])
    radii_ok = (
        len(cylinders) > 0
        and all(_close(item['radius'], radius, bbox_tol) for item in cylinders)
        and all(item['vertical'] for item in cylinders)
    )
    checks.append(_check(
        '{0}.target_cylinder_radii'.format(label), 'kernel',
        {
            'count': len(cylinders),
            'radii': [_num(item['radius']) for item in cylinders],
            'vertical': [item['vertical'] for item in cylinders],
        },
        {'count_min': 1, 'radius_mm': radius, 'vertical_axes': True},
        radii_ok,
        [] if radii_ok else ['real cylindrical face radii or axes differ from the design radius R'],
        {'bbox_mm': bbox_tol},
    ))
    return checks


def conservation_check(
    label: str,
    stock_roots: List[Any],
    pocket_roots: List[Any],
    target_roots: List[Any],
    tolerances: Dict[str, float],
) -> List[Dict[str, Any]]:
    '''Kernel volume conservation: target plus pocket equals stock. Scope: kernel.'''
    stock = measure_roots(stock_roots)
    pocket = measure_roots(pocket_roots)
    target = measure_roots(target_roots)
    tolerance = volume_tolerance(stock['volume'])
    combined = target['volume'] + pocket['volume']
    combined_ok = stock['finite'] and pocket['finite'] and target['finite']
    ok = combined_ok and _close(combined, stock['volume'], tolerance)
    return [_check(
        '{0}.volume_conservation'.format(label), 'kernel',
        {
            'stock_mm3': _num(stock['volume']),
            'target_plus_pocket_mm3': _num(combined),
            'abs_error_mm3': abs(combined - stock['volume']) if combined_ok else None,
        },
        {'abs_error_mm3': '<= {0}'.format(tolerance)},
        ok,
        [] if ok else ['target plus pocket volume does not conserve stock volume'],
        {'volume_abs_mm3': tolerances['volume_abs_mm3'], 'volume_rel': tolerances['volume_rel']},
    )]


def reimport_checks(label: str, preexport: Dict[str, Any], reimported: Dict[str, Any], tolerances: Dict[str, float]) -> List[Dict[str, Any]]:
    '''Compare reimported STEP measurements against preexport measurements. Scope: kernel.'''
    bbox_tol = float(tolerances['bbox_mm'])
    checks: List[Dict[str, Any]] = []

    count_ok = preexport['solid_count'] == reimported['solid_count']
    checks.append(_check(
        '{0}.reimport_solid_count'.format(label), 'kernel',
        {'preexport': preexport['solid_count'], 'reimport': reimported['solid_count']},
        {'equal': True}, count_ok,
        [] if count_ok else ['reimported solid count differs from preexport'],
        {'bbox_mm': bbox_tol},
    ))

    valid_ok = reimported['valid'] and reimported['finite']
    checks.append(_check(
        '{0}.reimport_validity'.format(label), 'kernel',
        {'reimport_valid': reimported['valid'], 'reimport_finite': reimported['finite']},
        {'reimport_valid': True, 'reimport_finite': True}, valid_ok,
        [] if valid_ok else ['reimported shape is invalid or reports non-finite values'],
        {'bbox_mm': bbox_tol},
    ))

    bbox_errors: Optional[Dict[str, float]] = None
    bbox_max_error: Optional[float] = None
    if preexport['bbox'] is not None and reimported['bbox'] is not None:
        bbox_errors = {
            key: abs(float(reimported['bbox'][key]) - float(preexport['bbox'][key]))
            for key in preexport['bbox']
        }
        bbox_max_error = max(bbox_errors.values())
    bbox_ok = _bbox_close(reimported['bbox'], preexport['bbox'] or {}, bbox_tol)
    checks.append(_check(
        '{0}.reimport_bbox'.format(label), 'kernel',
        {
            'preexport': preexport['bbox'],
            'reimport': reimported['bbox'],
            'abs_error_mm': bbox_errors,
            'max_abs_error_mm': bbox_max_error,
        },
        'all extrema within {0} mm'.format(bbox_tol), bbox_ok,
        [] if bbox_ok else ['reimported bounding box extrema differ from preexport beyond tolerance'],
        {'bbox_mm': bbox_tol},
    ))

    tolerance = volume_tolerance(preexport['volume'])
    volume_ok = preexport['finite'] and reimported['finite'] and _close(reimported['volume'], preexport['volume'], tolerance)
    volume_error: Optional[float] = None
    if preexport['finite'] and reimported['finite']:
        volume_error = abs(float(reimported['volume']) - float(preexport['volume']))
    checks.append(_check(
        '{0}.reimport_volume'.format(label), 'kernel',
        {
            'preexport_mm3': _num(preexport['volume']),
            'reimport_mm3': _num(reimported['volume']),
            'abs_error_mm3': volume_error,
        },
        {'abs_error_mm3': '<= {0}'.format(tolerance)}, volume_ok,
        [] if volume_ok else ['reimported volume differs from preexport beyond tolerance'],
        {'volume_abs_mm3': tolerances['volume_abs_mm3'], 'volume_rel': tolerances['volume_rel']},
    ))
    return checks


# ---------------------------------------------------------------------------
# records and artifacts
# ---------------------------------------------------------------------------


def _design_projection(record: Dict[str, Any]) -> Dict[str, Any]:
    '''Geometry identity projection: independent design parameters only.'''
    return {
        'schema_version': DESIGN_SCHEMA,
        'protocol_version': FROZEN_PROTOCOL_VERSION,
        'units': record['units'],
        'coordinates': record['coordinates'],
        'stock': record['stock'],
        'target': record['target'],
    }


def _case_record(
    case_id: str,
    family_id: str,
    ancestor_id: Optional[str],
    seed: int,
    stock: Dict[str, Any],
    target: Dict[str, Any],
    stock_artifact: Dict[str, Any],
    target_artifact: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        'schema_version': 'case-v1',
        'case_id': case_id,
        'version': 0,
        'family_id': family_id,
        'ancestor_id': ancestor_id,
        'seed': int(seed),
        'units': FROZEN_UNITS,
        'coordinates': dict(FROZEN_COORDINATES),
        'stock': {'L': stock['L'], 'W': stock['W'], 'H': stock['H']},
        'target': {
            'kind': target['kind'],
            'a': target['a'],
            'b': target['b'],
            'R': target['R'],
            'd': target['d'],
        },
        'artifacts': {
            'stock': {'path': stock_artifact['path'], 'sha256': stock_artifact['sha256']},
            'target': {'path': target_artifact['path'], 'sha256': target_artifact['sha256']},
        },
    }


def _prepare_output_root(root: Path) -> Path:
    if root.exists():
        if not root.is_dir():
            raise TargetError('output root exists and is not a directory', code='ERROR_INPUT')
        if any(root.iterdir()):
            raise TargetError('output root is not empty; refusing to overwrite previous artifacts', code='ERROR_INPUT')
    else:
        root.mkdir(parents=True, exist_ok=True)
    return root


def export_shape(shape: Any, path: Path) -> None:
    '''Export one shape as millimetre STEP.'''
    cq.exporters.export(shape, str(path), unit='MM', outputUnit='MM')


def reimport_step(path: Path) -> List[Any]:
    '''Reimport a STEP file and return every .vals() root, keeping compounds intact.'''
    workplane = cq.importers.importStep(str(path))
    roots = list(workplane.vals())
    if not roots:
        raise TargetError('reimport produced no roots for {0}'.format(Path(path).name))
    solids: List[Any] = []
    for root in roots:
        solids.extend(root.Solids())
    if not solids:
        raise TargetError('reimport produced no solids for {0}'.format(Path(path).name))
    if any(not solid.isValid() for solid in solids):
        raise TargetError('reimport produced an invalid solid for {0}'.format(Path(path).name))
    return roots


# ---------------------------------------------------------------------------
# previews (viewing aid only, never geometry verification)
# ---------------------------------------------------------------------------


def _ensure_mpl_config_dir() -> None:
    if not os.environ.get('MPLCONFIGDIR'):
        os.environ['MPLCONFIGDIR'] = tempfile.mkdtemp(prefix='g0-mplconfig-')


def _plot_shape(ax: Any, shape: Any, np: Any, poly_collection: Any) -> None:
    vertices, triangles = shape.tessellate(tolerance=0.1, angularTolerance=0.1)
    if not triangles:
        raise TargetError('preview tessellation produced no triangles')
    points = np.array([[vertex.x, vertex.y, vertex.z] for vertex in vertices], dtype=float)
    indices = np.asarray(triangles, dtype=int)
    faces = points[indices]
    normals = np.cross(faces[:, 1] - faces[:, 0], faces[:, 2] - faces[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    lengths[lengths == 0.0] = 1.0
    normals = normals / lengths[:, None]
    light = np.array([0.35, -0.55, 0.75])
    light = light / np.linalg.norm(light)
    shading = np.abs(normals @ light)
    base = np.array([0.30, 0.52, 0.78])
    colors = np.clip(base[None, :] * (0.35 + 0.65 * shading)[:, None], 0.0, 1.0)
    collection = poly_collection(faces, facecolors=colors, edgecolors='none', linewidths=0)
    collection.set_sort_zpos(None)
    ax.add_collection3d(collection)
    minima = points.min(axis=0)
    maxima = points.max(axis=0)
    center = (minima + maxima) / 2.0
    span = maxima - minima
    span[span <= 0.0] = 1.0
    radius = float(span.max()) / 2.0
    ax.set_xlim(center[0] - radius, center[0] + radius)
    ax.set_ylim(center[1] - radius, center[1] + radius)
    ax.set_zlim(center[2] - radius, center[2] + radius)
    ax.set_box_aspect((1.0, 1.0, 1.0))
    ax.view_init(elev=34, azim=-56)
    ax.set_xlabel('X mm')
    ax.set_ylabel('Y mm')
    ax.set_zlabel('Z mm')


def render_preview(shape: Any, design: Dict[str, Any], case_id: str, path: Path) -> None:
    '''Render the real tessellated target mesh to a per-case PNG.'''
    _ensure_mpl_config_dir()
    import matplotlib
    matplotlib.use('Agg', force=True)
    import matplotlib.pyplot as plt
    import numpy as np
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    figure = plt.figure(figsize=(6.4, 5.4), dpi=110)
    try:
        axes = figure.add_subplot(111, projection='3d')
        _plot_shape(axes, shape, np, Poly3DCollection)
        stock = design['stock']
        target = design['target']
        axes.set_title(
            '{0}  stock {1:g}x{2:g}x{3:g} mm\npocket a={4:g} b={5:g} R={6:g} d={7:g} mm'.format(
                case_id, stock['L'], stock['W'], stock['H'],
                target['a'], target['b'], target['R'], target['d'],
            ),
            fontsize=10,
        )
        figure.patch.set_facecolor('white')
        figure.savefig(str(path), dpi=110, facecolor='white', bbox_inches='tight')
    finally:
        plt.close(figure)


def render_overview(rows: List[Tuple[str, Path]], path: Path) -> None:
    '''Compose the per-case PNGs into a 2x3 style overview grid.'''
    if not rows:
        raise TargetError('overview needs at least one preview PNG')
    _ensure_mpl_config_dir()
    import matplotlib
    matplotlib.use('Agg', force=True)
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib import image as mpimg

    count = len(rows)
    columns = min(3, count)
    lines = int(math.ceil(count / float(columns)))
    figure, axes = plt.subplots(lines, columns, figsize=(5.0 * columns, 3.7 * lines), dpi=100)
    flat = np.atleast_1d(axes).ravel()
    try:
        for axis in flat:
            axis.axis('off')
        for axis, (case_id, png_path) in zip(flat, rows):
            axis.imshow(mpimg.imread(str(png_path)))
            axis.set_title(case_id, fontsize=11)
            axis.axis('off')
        figure.patch.set_facecolor('white')
        figure.tight_layout()
        figure.savefig(str(path), dpi=100, facecolor='white')
    finally:
        plt.close(figure)


# ---------------------------------------------------------------------------
# one case
# ---------------------------------------------------------------------------


def generate_case(
    spec: Dict[str, Any],
    output_root: Any,
    master_seed: int,
    *,
    preview: bool = True,
) -> Dict[str, Any]:
    '''Build, export, reimport and verify one target design.

    Writes case_id/validation.json always (once the case directory exists) and
    case_id/case.json only when every check passes. Throws TargetError only when
    the output directory cannot be prepared.
    '''
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    if not isinstance(spec, dict):
        raise TargetError('case spec must be a JSON object', code='ERROR_INPUT')
    spec_extra = set(spec) - _CASE_SPEC_KEYS
    if spec_extra:
        raise TargetError('unknown case spec keys: {0}'.format(sorted(spec_extra)), code='ERROR_INPUT')
    case_id = _safe_case_id(spec.get('case_id'))
    for nested_name, nested_allowed in (('stock', {'L', 'W', 'H'}), ('target', {'kind', 'a', 'b', 'R', 'd'})):
        nested = spec.get(nested_name)
        if not isinstance(nested, dict):
            raise TargetError('case spec {0} must be a JSON object'.format(nested_name), code='ERROR_INPUT')
        nested_extra = set(nested) - nested_allowed
        if nested_extra:
            raise TargetError('unknown {0} keys: {1}'.format(nested_name, sorted(nested_extra)), code='ERROR_INPUT')
    root = Path(output_root)
    family_id = spec.get('family_id') or FROZEN_FAMILY_ID
    if not isinstance(family_id, str) or not family_id:
        raise TargetError('family_id must be a non-empty string', code='ERROR_INPUT')
    case_dir = root / case_id
    resolved_root = root.resolve()
    resolved_case = case_dir.resolve()
    if resolved_case != resolved_root and resolved_root not in resolved_case.parents:
        raise TargetError('case_id escapes the output root', code='ERROR_INPUT')
    if case_dir.exists() and any(case_dir.iterdir()):
        raise TargetError('refusing to overwrite existing artifacts for case {0}'.format(case_id), code='ERROR_INPUT')
    case_dir.mkdir(parents=True, exist_ok=True)

    timings: Dict[str, Any] = {
        'build_s': None,
        'validate_s': None,
        'export_s': None,
        'reimport_s': None,
        'preview_s': None,
        'total_s': None,
        'process_total_s': None,
    }
    checks: List[Dict[str, Any]] = []
    error: Optional[str] = None
    reason_code: Optional[str] = None
    design_hash: Optional[str] = None
    record_hash: Optional[str] = None
    seed: Optional[int] = None
    design: Optional[Dict[str, Any]] = None
    artifact_rows: Dict[str, Any] = {}
    preview_rel: Optional[str] = None
    preview_error: Optional[str] = None
    case_rel: Optional[str] = None
    final_record: Optional[Dict[str, Any]] = None
    tolerances = dict(FROZEN_TOLERANCES)

    try:
        started = time.perf_counter()
        seed = derive_seed(master_seed, family_id, case_id)
        stock_rel = '{0}/stock.step'.format(case_id)
        target_rel = '{0}/target.step'.format(case_id)
        template = _case_record(
            case_id, family_id, spec.get('ancestor_id'), seed,
            spec.get('stock') or {}, spec.get('target') or {},
            {'path': stock_rel, 'sha256': '0' * 64},
            {'path': target_rel, 'sha256': '0' * 64},
        )
        normalized = validate_case(template)
        design = {'stock': dict(normalized['stock']), 'target': dict(normalized['target'])}
        design_hash = semantic_hash(_design_projection(normalized))
        timings['validate_s'] = time.perf_counter() - started

        started = time.perf_counter()
        built = build_design(design)
        timings['build_s'] = time.perf_counter() - started

        stock_path = case_dir / 'stock.step'
        target_path = case_dir / 'target.step'
        started = time.perf_counter()
        export_shape(built['stock'], stock_path)
        export_shape(built['target'], target_path)
        timings['export_s'] = time.perf_counter() - started
        for artifact_path in (stock_path, target_path):
            if not artifact_path.is_file() or artifact_path.stat().st_size == 0:
                raise TargetError('STEP artifact missing or empty: {0}'.format(artifact_path.name))

        stock_sha = file_checksum(stock_path)
        target_sha = file_checksum(target_path)
        artifact_rows = {
            'stock': {'path': stock_rel, 'sha256': stock_sha, 'size_bytes': int(stock_path.stat().st_size)},
            'target': {'path': target_rel, 'sha256': target_sha, 'size_bytes': int(target_path.stat().st_size)},
        }
        final_record = _case_record(
            case_id, family_id, spec.get('ancestor_id'), seed,
            normalized['stock'], normalized['target'],
            {'path': stock_rel, 'sha256': stock_sha},
            {'path': target_rel, 'sha256': target_sha},
        )
        started = time.perf_counter()
        final_record = validate_case(final_record)
        record_hash = semantic_hash(final_record)
        timings['validate_s'] = (timings['validate_s'] or 0.0) + (time.perf_counter() - started)

        started = time.perf_counter()
        imported_stock = reimport_step(stock_path)
        imported_target = reimport_step(target_path)
        timings['reimport_s'] = time.perf_counter() - started

        checks.extend(design_checks(design, tolerances))
        checks.extend(stock_checks('preexport', design, [built['stock']], tolerances))
        checks.extend(pocket_checks('preexport', design, [built['pocket']], tolerances))
        checks.extend(target_checks('preexport', design, [built['target']], tolerances))
        checks.extend(conservation_check('preexport', [built['stock']], [built['pocket']], [built['target']], tolerances))
        checks.extend(stock_checks('reimport', design, imported_stock, tolerances))
        checks.extend(target_checks('reimport', design, imported_target, tolerances))
        checks.extend(reimport_checks('stock', measure_roots([built['stock']]), measure_roots(imported_stock), tolerances))
        checks.extend(reimport_checks('target', measure_roots([built['target']]), measure_roots(imported_target), tolerances))

        if preview:
            started = time.perf_counter()
            try:
                render_preview(built['target'], design, case_id, case_dir / 'preview.png')
                if not (case_dir / 'preview.png').is_file():
                    raise TargetError('preview renderer did not write preview.png')
                preview_rel = '{0}/preview.png'.format(case_id)
            except Exception as exc:  # a requested preview that fails makes this case an ERROR
                preview_error = '{0}: {1}'.format(type(exc).__name__, exc)
                preview_rel = None
            timings['preview_s'] = time.perf_counter() - started
    except ContractError as exc:
        error = 'ContractError: {0}'.format(exc)
        reason_code = 'ERROR'
    except TargetError as exc:
        error = '{0}: {1}'.format(type(exc).__name__, exc)
        reason_code = exc.code
    except Exception as exc:
        error = '{0}: {1}'.format(type(exc).__name__, exc)
        reason_code = 'ERROR'

    if error is None:
        failed = [check for check in checks if not check['pass']]
        if failed:
            details = []
            for check in failed[:4]:
                message = check['errors'][0] if check['errors'] else 'failed'
                details.append('{0}: {1}'.format(check['name'], message))
            error = '; '.join(details)
            reason_code = 'FAIL'
    if preview_error is not None:
        preview_message = 'preview rendering failed: {0}'.format(preview_error)
        error = preview_message if error is None else '{0}; {1}'.format(error, preview_message)
        reason_code = 'ERROR'
    status = 'PASS' if error is None else _status_for_code(reason_code)

    timings['total_s'] = time.perf_counter() - wall_start
    timings['process_total_s'] = time.process_time() - cpu_start

    report = {
        'schema_version': VALIDATION_SCHEMA,
        'generator': {'id': GENERATOR_ID, 'version': GENERATOR_VERSION},
        'protocol_version': FROZEN_PROTOCOL_VERSION,
        'scope': VALIDATION_SCOPE,
        'case_id': case_id,
        'family_id': family_id,
        'seed': seed,
        'status': status,
        'error': error,
        'reason_code': reason_code,
        'hashes': {
            'input_design_semantic_hash': design_hash,
            'case_record_hash': record_hash,
        },
        'parameters': {'design': design, 'tolerances': dict(FROZEN_TOLERANCES)},
        'artifacts': artifact_rows,
        'checks': checks,
        'check_counts': {
            'total': len(checks),
            'passed': sum(1 for check in checks if check['pass']),
        },
        'preview': {
            'rendered': preview_rel is not None,
            'path': preview_rel,
            'error': preview_error,
            'note': 'tessellated viewing aid; not geometry verification',
        },
        'limitations': [
            'target-validation-v1 covers target geometry design/kernel consistency only',
            '0.001 mm and the volume formula are numerical consistency bounds, not Q3 manufacturing tolerance',
            'no toolpath, sweep, material-removal simulation, candidate, budget or manufacturing claim is produced',
        ],
        'timings': timings,
    }
    _write_json(case_dir / 'validation.json', report)
    if status == 'PASS' and final_record is not None:
        _write_json(case_dir / 'case.json', final_record)
        case_rel = '{0}/case.json'.format(case_id)

    return {
        'case_id': case_id,
        'family_id': family_id,
        'status': status,
        'error': error,
        'reason_code': reason_code,
        'seed': seed,
        'design_semantic_hash': design_hash,
        'case_record_hash': record_hash,
        'case_path': case_rel,
        'validation_path': '{0}/validation.json'.format(case_id),
        'preview_path': preview_rel,
        'preview_error': preview_error,
        'artifacts': artifact_rows,
        'checks_total': len(checks),
        'checks_passed': sum(1 for check in checks if check['pass']),
        'timings': timings,
    }


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------


def _manifest_row(result: Dict[str, Any], root: Path) -> Dict[str, Any]:
    preview_path = result.get('preview_path')
    return {
        'case_id': result.get('case_id'),
        'status': result.get('status'),
        'error': result.get('error'),
        'reason_code': result.get('reason_code'),
        'seed': result.get('seed'),
        'design_semantic_hash': result.get('design_semantic_hash'),
        'case_record_hash': result.get('case_record_hash'),
        'case_path': result.get('case_path'),
        'validation_path': result.get('validation_path'),
        'preview_path': preview_path if preview_path and (root / preview_path).is_file() else None,
        'artifacts': result.get('artifacts') or {},
        'checks_total': result.get('checks_total'),
        'checks_passed': result.get('checks_passed'),
        'timings': result.get('timings'),
    }


def generate_batch(
    config: Dict[str, Any],
    output_root: Any,
    master_seed: Optional[int] = None,
    *,
    preview: bool = True,
    case_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    '''Generate every selected case, then write the config snapshot and manifest.'''
    enforce_frozen = isinstance(config, dict) and config.get('config_version') == FROZEN_CONFIG_VERSION
    _validate_config(config, enforce_frozen=enforce_frozen)
    if enforce_frozen and master_seed is not None and master_seed != FROZEN_MASTER_SEED:
        raise TargetError(
            'master_seed override is not allowed for {0}'.format(FROZEN_CONFIG_VERSION),
            code='ERROR_INPUT',
        )
    master = _validate_master_seed(config['master_seed'] if master_seed is None else master_seed)
    root = _prepare_output_root(Path(output_root))
    available = {str(row['case_id']) for row in config['cases']}
    if case_ids is not None:
        missing = [case_id for case_id in case_ids if case_id not in available]
        if missing:
            raise TargetError('unknown case id(s): {0}'.format(', '.join(missing)), code='ERROR_INPUT')
    selected = [row for row in config['cases'] if case_ids is None or str(row['case_id']) in set(case_ids)]

    snapshot = copy.deepcopy(config)
    snapshot['master_seed'] = master
    _write_json(root / 'config_snapshot.json', snapshot)

    rows: List[Dict[str, Any]] = []
    for row in selected:
        try:
            rows.append(generate_case(row, root, master, preview=preview))
        except TargetError as exc:
            rows.append({
                'case_id': str(row.get('case_id')),
                'family_id': row.get('family_id'),
                'status': _status_for_code(exc.code),
                'error': '{0}: {1}'.format(type(exc).__name__, exc),
                'reason_code': exc.code,
                'seed': None,
                'design_semantic_hash': None,
                'case_record_hash': None,
                'case_path': None,
                'validation_path': None,
                'preview_path': None,
                'preview_error': None,
                'artifacts': {},
                'checks_total': 0,
                'checks_passed': 0,
                'timings': {},
            })
        except Exception as exc:
            rows.append({
                'case_id': str(row.get('case_id')),
                'family_id': row.get('family_id'),
                'status': 'ERROR',
                'error': '{0}: {1}'.format(type(exc).__name__, exc),
                'reason_code': 'ERROR',
                'seed': None,
                'design_semantic_hash': None,
                'case_record_hash': None,
                'case_path': None,
                'validation_path': None,
                'preview_path': None,
                'preview_error': None,
                'artifacts': {},
                'checks_total': 0,
                'checks_passed': 0,
                'timings': {},
            })

    overview_rel: Optional[str] = None
    overview_error: Optional[str] = None
    if preview:
        pairs = [
            (str(result['case_id']), root / str(result['preview_path']))
            for result in rows
            if result.get('preview_path') and (root / result['preview_path']).is_file()
        ]
        if pairs:
            try:
                render_overview(pairs, root / 'overview.png')
                overview_rel = 'overview.png'
            except Exception as exc:  # a requested viewing artifact that fails makes the delivery ERROR
                overview_error = '{0}: {1}'.format(type(exc).__name__, exc)
                overview_rel = None
        else:
            overview_error = 'overview requested but no preview PNG was produced'

    statuses = [result['status'] for result in rows]
    if not rows:
        overall = 'FAIL'
        batch_error = 'no cases were selected'
    elif overview_error is not None or any(item in ('ERROR', 'UNKNOWN') for item in statuses):
        overall = 'ERROR'
        batch_error = overview_error or next(
            (result['error'] for result in rows if result['status'] in ('ERROR', 'UNKNOWN')), None
        )
    elif any(item != 'PASS' for item in statuses):
        overall = 'FAIL'
        batch_error = next((result['error'] for result in rows if result['status'] != 'PASS'), None)
    else:
        overall = 'PASS'
        batch_error = None

    manifest = {
        'schema_version': MANIFEST_SCHEMA,
        'generator': {'id': GENERATOR_ID, 'version': GENERATOR_VERSION},
        'protocol_version': FROZEN_PROTOCOL_VERSION,
        'config_version': config.get('config_version'),
        'family_id': config.get('family_id'),
        'master_seed': master,
        'seed_rule': FROZEN_SEED_RULE,
        'units': FROZEN_UNITS,
        'tolerances': dict(FROZEN_TOLERANCES),
        'status': overall,
        'error': batch_error,
        'config_snapshot_path': 'config_snapshot.json',
        'overview_path': overview_rel,
        'cases': [_manifest_row(result, root) for result in rows],
    }
    _write_json(root / 'manifest.json', manifest)
    return {
        'status': overall,
        'error': batch_error,
        'root': str(root),
        'manifest_path': 'manifest.json',
        'config_snapshot_path': 'config_snapshot.json',
        'overview_path': overview_rel,
        'cases': rows,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m pilot.targets',
        description='Generate G0 target STEP artifacts with a target-validation-v1 report.',
    )
    parser.add_argument('--config', required=True, help='frozen target config JSON')
    parser.add_argument('--case', default=None, help='single case id; omit to generate all cases')
    parser.add_argument('--out', required=True, help='new or empty output root')
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    '''CLI entry point: 0 all PASS, 1 verification failure, 2 config or usage error.'''
    args = build_arg_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except Exception as exc:  # config errors are usage errors and exit non-zero
        print('error: {0}'.format(exc), file=sys.stderr)
        return 2
    case_ids = None if args.case is None else [args.case]
    try:
        result = generate_batch(
            config,
            args.out,
            master_seed=int(config['master_seed']),
            preview=True,
            case_ids=case_ids,
        )
    except Exception as exc:  # output preparation errors are usage errors
        print('error: {0}'.format(exc), file=sys.stderr)
        return 2
    for row in result['cases']:
        suffix = ' ({0})'.format(row['error']) if row.get('error') else ''
        print('{0}: {1}{2}'.format(row['case_id'], row['status'], suffix))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
