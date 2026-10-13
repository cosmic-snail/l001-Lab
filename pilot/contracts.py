'''Versioned JSON data contracts for the l001-Lab protocol v1.0.

The module defines the four top-level records (case, candidate, evidence, run),
their validation entry points, the canonical JSON encoder, and the two distinct
hash APIs. It uses only the Python standard library. It never evaluates geometry,
reads artifact bytes during validation, or derives a status from candidate data.
'''

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, TypedDict

__all__ = [
    'ContractError',
    'SCHEMA_CASE',
    'SCHEMA_CANDIDATE',
    'SCHEMA_EVIDENCE',
    'SCHEMA_RUN',
    'canonical_json',
    'file_checksum',
    'parse_record',
    'semantic_hash',
    'serialize_record',
    'validate_case',
    'validate_candidate',
    'validate_evidence',
    'validate_run',
]

SCHEMA_CASE = 'case-v1'
SCHEMA_CANDIDATE = 'candidate-v1'
SCHEMA_EVIDENCE = 'evidence-v1'
SCHEMA_RUN = 'run-v1'

UNITS_MM = 'mm'
PROTOCOL_VERSION = '1.0'

SEMANTIC_DOMAIN = b'l001-lab/data-contracts/semantic/v1' + bytes(1)
CHECKSUM_CHUNK = 1 << 16
UINT64_MAX = (1 << 64) - 1

LOWERHEX64 = re.compile('^[0-9a-f]{64}$')
UTC_ISO8601 = re.compile(
    '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}'
    '([.][0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$'
)

MOTION_MODES = ('CUT_XY', 'CUT_Z', 'RAPID_XY', 'RAPID_Z')
CHECK_IDS = ('Q1', 'Q2', 'Q3')

STATUS_REASONS: Dict[str, tuple] = {
    'PASS': ('OK',),
    'FAIL': ('GEOMETRY_FAIL',),
    'UNKNOWN': ('UNKNOWN_NUMERIC', 'UNKNOWN_UNSUPPORTED', 'UNKNOWN_TIMEOUT'),
    'ERROR': ('ERROR_INPUT', 'ERROR_KERNEL', 'ERROR_PROCESS'),
}

CHECK_DEPENDENCIES: Dict[str, tuple] = {
    'Q1': ('tools', 'path', 'fixtures', 'coordinates'),
    'Q2': ('stock', 'tools', 'path', 'coordinates', 'prior_state'),
    'Q3': ('stock', 'target', 'tools', 'path', 'coordinates'),
}


class ContractError(Exception):
    '''A validation failure with a coarse status and a specific reason code.'''

    def __init__(self, status: str, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.reason_code = reason_code
        self.message = message


class Artifact(TypedDict):
    path: str
    sha256: str


class Coordinates(TypedDict):
    handedness: str
    up: str
    top_z: int
    tool_axis: List[int]


class Stock(TypedDict):
    L: float
    W: float
    H: float


class Target(TypedDict):
    kind: str
    a: float
    b: float
    R: float
    d: float


class CaseArtifacts(TypedDict):
    stock: Artifact
    target: Artifact


class Case(TypedDict):
    schema_version: str
    case_id: str
    version: int
    family_id: str
    ancestor_id: Optional[str]
    seed: int
    units: str
    coordinates: Coordinates
    stock: Stock
    target: Target
    artifacts: CaseArtifacts


class Tool(TypedDict):
    tool_id: str
    radius: float
    cutting_length: float
    axis: List[int]


class Motion(TypedDict):
    start: List[float]
    end: List[float]
    tool_id: str
    operation_id: str
    mode: str


class Candidate(TypedDict):
    schema_version: str
    candidate_id: str
    case_id: str
    version: int
    case_hash: str
    units: str
    tools: List[Tool]
    path: List[Motion]


class Evidence(TypedDict):
    schema_version: str
    case_id: str
    candidate_id: str
    version: int
    check_id: str
    status: str
    reason_code: str
    metrics: Dict[str, Any]
    dependency_hashes: Dict[str, str]
    checker_version: str
    tolerance_version: str
    state_hash: str
    wall_seconds: float
    cpu_seconds: float
    input_artifacts: List[Artifact]
    output_artifacts: List[Artifact]


class Run(TypedDict):
    schema_version: str
    run_id: str
    protocol_version: str
    seed: int
    case_hashes: Dict[str, str]
    candidate_hashes: Dict[str, str]
    config_hash: str
    checker_version: str
    tolerance_version: str
    started_at: str
    mode: str


def _fail(reason_code: str, message: str) -> None:
    status = 'ERROR' if reason_code.startswith('ERROR') else 'UNKNOWN'
    raise ContractError(status, reason_code, message)


def _input_error(message: str) -> None:
    _fail('ERROR_INPUT', message)


def _unsupported(message: str) -> None:
    _fail('UNKNOWN_UNSUPPORTED', message)


def _as_mapping(value: Any, field: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        _input_error(field + ' must be a JSON object')
    return value


def _check_keys(obj: Dict[str, Any], required: tuple, field: str) -> None:
    for key in required:
        if key not in obj:
            _input_error(field + ' is missing required key ' + repr(key))
    for key in obj:
        if key not in required:
            _input_error(field + ' has unknown key ' + repr(key))


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        _input_error(field + ' must be a nonempty string')
    return value


def _enum(value: Any, allowed: tuple, field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        _input_error(field + ' must be one of ' + repr(allowed))
    return value


def _int(value: Any, field: str, minimum: Optional[int] = None,
         maximum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _input_error(field + ' must be an integer')
    if minimum is not None and value < minimum:
        _input_error(field + ' must be at least ' + str(minimum))
    if maximum is not None and value > maximum:
        _input_error(field + ' must be at most ' + str(maximum))
    return value


def _normalize_number(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if value == 0.0:
        return 0
    if value.is_integer():
        return int(value)
    return value


def _number(value: Any, field: str) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _input_error(field + ' must be a finite number')
    if isinstance(value, float) and not math.isfinite(value):
        _input_error(field + ' must be a finite number')
    return _normalize_number(value)


def _positive(value: Any, field: str) -> Any:
    number = _number(value, field)
    if number <= 0:
        _input_error(field + ' must be positive')
    return number


def _nonnegative(value: Any, field: str) -> Any:
    number = _number(value, field)
    if number < 0:
        _input_error(field + ' must be non-negative')
    return number


def _lowerhex64(value: Any, field: str) -> str:
    text = _string(value, field)
    if LOWERHEX64.fullmatch(text) is None:
        _input_error(field + ' must be a 64-character lowercase hex string')
    return text


def _units(value: Any, field: str) -> str:
    text = _string(value, field)
    if text != UNITS_MM:
        _input_error(field + ' must be ' + repr(UNITS_MM))
    return UNITS_MM


def _xyz(value: Any, field: str) -> List[Any]:
    if not isinstance(value, list) or len(value) != 3:
        _input_error(field + ' must be an array of three numbers')
    return [_number(item, field + '[' + str(index) + ']') for index, item in enumerate(value)]


def _vertical_axis(value: Any, field: str) -> List[int]:
    axis = _xyz(value, field)
    if axis != [0, 0, 1]:
        _unsupported(field + ' must be the vertical tool axis [0, 0, 1]')
    return [0, 0, 1]


def _portable_path(value: Any, field: str) -> str:
    text = _string(value, field)
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        _input_error(field + ' must not contain ASCII control characters')
    if chr(92) in text:
        _input_error(field + ' must use portable forward slashes')
    if text.startswith('/'):
        _input_error(field + ' must be relative, not absolute')
    if re.match('^[A-Za-z]:', text) is not None:
        _input_error(field + ' must not be a drive path')
    parts = text.split('/')
    for part in parts:
        if part in ('', '.', '..'):
            _input_error(field + ' must not contain empty, dot, or traversal components')
    return '/'.join(parts)


def _artifact(value: Any, field: str) -> Artifact:
    obj = _as_mapping(value, field)
    _check_keys(obj, ('path', 'sha256'), field)
    return {
        'path': _portable_path(obj['path'], field + '.path'),
        'sha256': _lowerhex64(obj['sha256'], field + '.sha256'),
    }


def _artifact_list(value: Any, field: str) -> List[Artifact]:
    if not isinstance(value, list):
        _input_error(field + ' must be an array')
    return [
        _artifact(item, field + '[' + str(index) + ']')
        for index, item in enumerate(value)
    ]


def _hash_map(value: Any, field: str) -> Dict[str, str]:
    obj = _as_mapping(value, field)
    if not obj:
        _input_error(field + ' must not be empty')
    out: Dict[str, str] = {}
    for key, raw in obj.items():
        if not isinstance(key, str) or not key:
            _input_error(field + ' keys must be nonempty strings')
        out[key] = _lowerhex64(raw, field + '[' + repr(key) + ']')
    return out


_COORDINATE_KEYS = ('handedness', 'up', 'top_z', 'tool_axis')
_STOCK_KEYS = ('L', 'W', 'H')
_TARGET_KEYS = ('kind', 'a', 'b', 'R', 'd')
_CASE_KEYS = (
    'schema_version',
    'case_id',
    'version',
    'family_id',
    'ancestor_id',
    'seed',
    'units',
    'coordinates',
    'stock',
    'target',
    'artifacts',
)
_CANDIDATE_KEYS = (
    'schema_version',
    'candidate_id',
    'case_id',
    'version',
    'case_hash',
    'units',
    'tools',
    'path',
)
_TOOL_KEYS = ('tool_id', 'radius', 'cutting_length', 'axis')
_MOTION_KEYS = ('start', 'end', 'tool_id', 'operation_id', 'mode')
_EVIDENCE_KEYS = (
    'schema_version',
    'case_id',
    'candidate_id',
    'version',
    'check_id',
    'status',
    'reason_code',
    'metrics',
    'dependency_hashes',
    'checker_version',
    'tolerance_version',
    'state_hash',
    'wall_seconds',
    'cpu_seconds',
    'input_artifacts',
    'output_artifacts',
)
_RUN_KEYS = (
    'schema_version',
    'run_id',
    'protocol_version',
    'seed',
    'case_hashes',
    'candidate_hashes',
    'config_hash',
    'checker_version',
    'tolerance_version',
    'started_at',
    'mode',
)


def _coordinates(value: Any) -> Coordinates:
    obj = _as_mapping(value, 'case.coordinates')
    _check_keys(obj, _COORDINATE_KEYS, 'case.coordinates')
    if obj['handedness'] != 'right':
        _input_error('case.coordinates.handedness must be right')
    up = _string(obj['up'], 'case.coordinates.up')
    if up not in ('Z', 'z'):
        _input_error('case.coordinates.up must be Z')
    top_z = _number(obj['top_z'], 'case.coordinates.top_z')
    if top_z != 0:
        _input_error('case.coordinates.top_z must be 0')
    return {
        'handedness': 'right',
        'up': 'Z',
        'top_z': 0,
        'tool_axis': _vertical_axis(obj['tool_axis'], 'case.coordinates.tool_axis'),
    }


def _stock(value: Any, field: str) -> Stock:
    obj = _as_mapping(value, field)
    _check_keys(obj, _STOCK_KEYS, field)
    return {
        'L': _positive(obj['L'], field + '.L'),
        'W': _positive(obj['W'], field + '.W'),
        'H': _positive(obj['H'], field + '.H'),
    }


def _target(value: Any, stock: Stock) -> Target:
    obj = _as_mapping(value, 'case.target')
    _check_keys(obj, _TARGET_KEYS, 'case.target')
    if obj['kind'] != 'rounded_rect_blind_pocket':
        _input_error('case.target.kind must be rounded_rect_blind_pocket')
    a = _positive(obj['a'], 'case.target.a')
    b = _positive(obj['b'], 'case.target.b')
    radius = _positive(obj['R'], 'case.target.R')
    depth = _positive(obj['d'], 'case.target.d')
    if a >= stock['L'] or b >= stock['W']:
        _input_error('case.target must be strictly contained in the stock footprint')
    if radius > 0.5 * min(a, b):
        _input_error('case.target.R must be at most half of min(a, b)')
    if depth >= stock['H']:
        _input_error('case.target.d must be less than stock.H')
    return {'kind': 'rounded_rect_blind_pocket', 'a': a, 'b': b, 'R': radius, 'd': depth}


def validate_case(value: Any) -> Case:
    '''Validate a case record and return an owned normalized copy.'''
    obj = _as_mapping(value, 'case')
    _check_keys(obj, _CASE_KEYS, 'case')
    if obj['schema_version'] != SCHEMA_CASE:
        _input_error('case.schema_version must be ' + repr(SCHEMA_CASE))
    case_id = _string(obj['case_id'], 'case.case_id')
    version = _int(obj['version'], 'case.version', minimum=0)
    family_id = _string(obj['family_id'], 'case.family_id')
    ancestor_id = obj['ancestor_id']
    if ancestor_id is not None:
        ancestor_id = _string(ancestor_id, 'case.ancestor_id')
    seed = _int(obj['seed'], 'case.seed', minimum=0, maximum=UINT64_MAX)
    units = _units(obj['units'], 'case.units')
    coordinates = _coordinates(obj['coordinates'])
    stock = _stock(obj['stock'], 'case.stock')
    target = _target(obj['target'], stock)
    artifacts = _as_mapping(obj['artifacts'], 'case.artifacts')
    _check_keys(artifacts, ('stock', 'target'), 'case.artifacts')
    return {
        'schema_version': SCHEMA_CASE,
        'case_id': case_id,
        'version': version,
        'family_id': family_id,
        'ancestor_id': ancestor_id,
        'seed': seed,
        'units': units,
        'coordinates': coordinates,
        'stock': stock,
        'target': target,
        'artifacts': {
            'stock': _artifact(artifacts['stock'], 'case.artifacts.stock'),
            'target': _artifact(artifacts['target'], 'case.artifacts.target'),
        },
    }


def _tool(value: Any, field: str) -> Tool:
    obj = _as_mapping(value, field)
    _check_keys(obj, _TOOL_KEYS, field)
    return {
        'tool_id': _string(obj['tool_id'], field + '.tool_id'),
        'radius': _positive(obj['radius'], field + '.radius'),
        'cutting_length': _positive(obj['cutting_length'], field + '.cutting_length'),
        'axis': _vertical_axis(obj['axis'], field + '.axis'),
    }


def _motion(value: Any, field: str, tool_ids: set) -> Motion:
    obj = _as_mapping(value, field)
    _check_keys(obj, _MOTION_KEYS, field)
    start = _xyz(obj['start'], field + '.start')
    end = _xyz(obj['end'], field + '.end')
    tool_id = _string(obj['tool_id'], field + '.tool_id')
    if tool_id not in tool_ids:
        _input_error(field + '.tool_id does not reference a defined tool')
    operation_id = _string(obj['operation_id'], field + '.operation_id')
    mode = obj['mode']
    if not isinstance(mode, str):
        _input_error(field + '.mode must be a string')
    if mode not in MOTION_MODES:
        _unsupported(field + '.mode must be one of ' + repr(MOTION_MODES))
    if mode.endswith('_XY'):
        if start[2] != end[2]:
            _unsupported(field + ' uses an XY mode with a changing Z (diagonal movement)')
    elif start[0] != end[0] or start[1] != end[1]:
        _unsupported(field + ' uses a Z mode with changing X/Y (diagonal movement)')
    return {
        'start': start,
        'end': end,
        'tool_id': tool_id,
        'operation_id': operation_id,
        'mode': mode,
    }


def validate_candidate(value: Any) -> Candidate:
    '''Validate a candidate record and return an owned normalized copy.'''
    obj = _as_mapping(value, 'candidate')
    _check_keys(obj, _CANDIDATE_KEYS, 'candidate')
    if obj['schema_version'] != SCHEMA_CANDIDATE:
        _input_error('candidate.schema_version must be ' + repr(SCHEMA_CANDIDATE))
    candidate_id = _string(obj['candidate_id'], 'candidate.candidate_id')
    case_id = _string(obj['case_id'], 'candidate.case_id')
    version = _int(obj['version'], 'candidate.version', minimum=0)
    case_hash = _lowerhex64(obj['case_hash'], 'candidate.case_hash')
    units = _units(obj['units'], 'candidate.units')
    tools_raw = obj['tools']
    if not isinstance(tools_raw, list) or not tools_raw:
        _input_error('candidate.tools must be a nonempty array')
    tools: List[Tool] = []
    tool_ids = set()
    for index, raw in enumerate(tools_raw):
        tool = _tool(raw, 'candidate.tools[' + str(index) + ']')
        if tool['tool_id'] in tool_ids:
            _input_error('candidate.tools contains duplicate tool_id ' + repr(tool['tool_id']))
        tool_ids.add(tool['tool_id'])
        tools.append(tool)
    path_raw = obj['path']
    if not isinstance(path_raw, list) or not path_raw:
        _input_error('candidate.path must be a nonempty array')
    path: List[Motion] = []
    for index, raw in enumerate(path_raw):
        path.append(_motion(raw, 'candidate.path[' + str(index) + ']', tool_ids))
    for index in range(1, len(path)):
        if path[index - 1]['end'] != path[index]['start']:
            _input_error('candidate.path must be continuous between consecutive segments')
    return {
        'schema_version': SCHEMA_CANDIDATE,
        'candidate_id': candidate_id,
        'case_id': case_id,
        'version': version,
        'case_hash': case_hash,
        'units': units,
        'tools': tools,
        'path': path,
    }


def _metric_value(value: Any, field: str) -> Any:
    if isinstance(value, bool):
        _input_error(field + ' must be a finite number or an array of finite numbers')
    if isinstance(value, (int, float)):
        return _number(value, field)
    if isinstance(value, list):
        return [
            _number(item, field + '[' + str(index) + ']')
            for index, item in enumerate(value)
        ]
    _input_error(field + ' must be a finite number or an array of finite numbers')


def _metrics(value: Any) -> Dict[str, Any]:
    obj = _as_mapping(value, 'evidence.metrics')
    out: Dict[str, Any] = {}
    for key, raw in obj.items():
        if not isinstance(key, str):
            _input_error('evidence.metrics keys must be strings')
        out[key] = _metric_value(raw, 'evidence.metrics[' + repr(key) + ']')
    return out


def _dependency_hashes(value: Any, check_id: str) -> Dict[str, str]:
    obj = _as_mapping(value, 'evidence.dependency_hashes')
    if not obj:
        _input_error('evidence.dependency_hashes must not be empty')
    out: Dict[str, str] = {}
    for key, raw in obj.items():
        if not isinstance(key, str) or not key:
            _input_error('evidence.dependency_hashes keys must be nonempty strings')
        out[key] = _lowerhex64(raw, 'evidence.dependency_hashes[' + repr(key) + ']')
    missing = [key for key in CHECK_DEPENDENCIES[check_id] if key not in out]
    if missing:
        _input_error(
            'evidence.dependency_hashes for ' + check_id + ' is missing ' + repr(missing)
        )
    return out


def validate_evidence(value: Any) -> Evidence:
    '''Validate an evidence record and return an owned normalized copy.'''
    obj = _as_mapping(value, 'evidence')
    _check_keys(obj, _EVIDENCE_KEYS, 'evidence')
    if obj['schema_version'] != SCHEMA_EVIDENCE:
        _input_error('evidence.schema_version must be ' + repr(SCHEMA_EVIDENCE))
    case_id = _string(obj['case_id'], 'evidence.case_id')
    candidate_id = _string(obj['candidate_id'], 'evidence.candidate_id')
    version = _int(obj['version'], 'evidence.version', minimum=0)
    check_id = _enum(obj['check_id'], CHECK_IDS, 'evidence.check_id')
    status = obj['status']
    if not isinstance(status, str) or status not in STATUS_REASONS:
        _input_error('evidence.status must be one of ' + repr(tuple(STATUS_REASONS)))
    reason_code = obj['reason_code']
    if not isinstance(reason_code, str) or reason_code not in STATUS_REASONS[status]:
        _input_error('evidence.reason_code ' + repr(reason_code) + ' is not valid for status ' + status)
    metrics = _metrics(obj['metrics'])
    dependency_hashes = _dependency_hashes(obj['dependency_hashes'], check_id)
    checker_version = _string(obj['checker_version'], 'evidence.checker_version')
    tolerance_version = _string(obj['tolerance_version'], 'evidence.tolerance_version')
    state_hash = _lowerhex64(obj['state_hash'], 'evidence.state_hash')
    wall_seconds = _nonnegative(obj['wall_seconds'], 'evidence.wall_seconds')
    cpu_seconds = _nonnegative(obj['cpu_seconds'], 'evidence.cpu_seconds')
    input_artifacts = _artifact_list(obj['input_artifacts'], 'evidence.input_artifacts')
    output_artifacts = _artifact_list(obj['output_artifacts'], 'evidence.output_artifacts')
    return {
        'schema_version': SCHEMA_EVIDENCE,
        'case_id': case_id,
        'candidate_id': candidate_id,
        'version': version,
        'check_id': check_id,
        'status': status,
        'reason_code': reason_code,
        'metrics': metrics,
        'dependency_hashes': dependency_hashes,
        'checker_version': checker_version,
        'tolerance_version': tolerance_version,
        'state_hash': state_hash,
        'wall_seconds': wall_seconds,
        'cpu_seconds': cpu_seconds,
        'input_artifacts': input_artifacts,
        'output_artifacts': output_artifacts,
    }


def _utc_timestamp(value: Any, field: str) -> str:
    text = _string(value, field)
    if UTC_ISO8601.match(text) is None:
        _input_error(field + ' must be an ISO8601 timestamp with a UTC offset')
    normalized = text.replace(' ', 'T')
    if normalized.endswith('Z'):
        normalized = normalized[:-1] + '+00:00'
    try:
        stamp = datetime.fromisoformat(normalized)
    except ValueError:
        _input_error(field + ' must be a valid ISO8601 timestamp')
    if stamp.utcoffset() != timedelta(0):
        _input_error(field + ' must be expressed in UTC')
    return stamp.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def validate_run(value: Any) -> Run:
    '''Validate a run record and return an owned normalized copy.'''
    obj = _as_mapping(value, 'run')
    _check_keys(obj, _RUN_KEYS, 'run')
    if obj['schema_version'] != SCHEMA_RUN:
        _input_error('run.schema_version must be ' + repr(SCHEMA_RUN))
    run_id = _string(obj['run_id'], 'run.run_id')
    if obj['protocol_version'] != PROTOCOL_VERSION:
        _input_error('run.protocol_version must be ' + repr(PROTOCOL_VERSION))
    seed = _int(obj['seed'], 'run.seed', minimum=0, maximum=UINT64_MAX)
    case_hashes = _hash_map(obj['case_hashes'], 'run.case_hashes')
    candidate_hashes = _hash_map(obj['candidate_hashes'], 'run.candidate_hashes')
    config_hash = _lowerhex64(obj['config_hash'], 'run.config_hash')
    checker_version = _string(obj['checker_version'], 'run.checker_version')
    tolerance_version = _string(obj['tolerance_version'], 'run.tolerance_version')
    started_at = _utc_timestamp(obj['started_at'], 'run.started_at')
    mode = _enum(obj['mode'], ('P1-A', 'P1-S'), 'run.mode')
    return {
        'schema_version': SCHEMA_RUN,
        'run_id': run_id,
        'protocol_version': PROTOCOL_VERSION,
        'seed': seed,
        'case_hashes': case_hashes,
        'candidate_hashes': candidate_hashes,
        'config_hash': config_hash,
        'checker_version': checker_version,
        'tolerance_version': tolerance_version,
        'started_at': started_at,
        'mode': mode,
    }


def _write_canonical(value: Any, out: List[str]) -> None:
    if value is None:
        out.append('null')
        return
    if value is True:
        out.append('true')
        return
    if value is False:
        out.append('false')
        return
    if isinstance(value, str):
        out.append(json.dumps(value, ensure_ascii=False))
        return
    if isinstance(value, int):
        out.append(str(value))
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _input_error('canonical_json rejects non-finite numbers')
        out.append(str(_normalize_number(value)))
        return
    if isinstance(value, (list, tuple)):
        out.append('[')
        for index, item in enumerate(value):
            if index:
                out.append(',')
            _write_canonical(item, out)
        out.append(']')
        return
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                _input_error('canonical_json requires string mapping keys')
        out.append('{')
        for index, key in enumerate(sorted(value)):
            if index:
                out.append(',')
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(':')
            _write_canonical(value[key], out)
        out.append('}')
        return
    _input_error('canonical_json rejects ' + type(value).__name__ + ' values')


def canonical_json(value: Any) -> str:
    '''Return the deterministic compact UTF-8 JSON encoding of a JSON value.'''
    out: List[str] = []
    _write_canonical(value, out)
    return ''.join(out)


def semantic_hash(value: Any) -> str:
    '''Return the domain-separated SHA256 of canonical JSON for a semantic value.'''
    payload = SEMANTIC_DOMAIN + canonical_json(value).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def file_checksum(path: Any) -> str:
    '''Return the SHA256 of raw file bytes, read in fixed-size chunks.'''
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(CHECKSUM_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _reject_duplicate_keys(pairs: List[tuple]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _input_error('duplicate JSON object key ' + repr(key))
        result[key] = value
    return result


def _reject_constant(token: str) -> Any:
    _input_error('JSON constant ' + repr(token) + ' is not allowed')


def _parse_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        _input_error('JSON number ' + repr(token) + ' is not finite')
    return value


def _loads_strict(text: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
        )
    except ContractError:
        raise
    except ValueError as exc:
        _input_error('record JSON is malformed: ' + str(exc))


_RECORDS: Dict[str, tuple] = {
    'case': (SCHEMA_CASE, validate_case),
    'candidate': (SCHEMA_CANDIDATE, validate_candidate),
    'evidence': (SCHEMA_EVIDENCE, validate_evidence),
    'run': (SCHEMA_RUN, validate_run),
}


def _record_schema(kind: Any) -> tuple:
    if not isinstance(kind, str) or kind not in _RECORDS:
        _input_error('unknown record kind ' + repr(kind))
    return _RECORDS[kind]


def serialize_record(kind: str, record: Any) -> str:
    '''Validate a record and return its canonical JSON text.'''
    schema_version, validator = _record_schema(kind)
    normalized = validator(record)
    if normalized.get('schema_version') != schema_version:
        _input_error('record does not match schema ' + repr(schema_version))
    return canonical_json(normalized)


def parse_record(kind: str, text: Any) -> Any:
    '''Parse JSON text into a validated, normalized, owned record.'''
    schema_version, validator = _record_schema(kind)
    if not isinstance(text, str):
        _input_error('record text must be a string')
    data = _loads_strict(text)
    if not isinstance(data, dict):
        _input_error('record JSON must be an object')
    if data.get('schema_version') != schema_version:
        _input_error('record must use schema_version ' + repr(schema_version))
    return validator(data)
