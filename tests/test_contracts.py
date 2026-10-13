'''Contract tests for the l001-Lab v1 data contracts.'''

from __future__ import annotations

import copy
import json

import pytest

from pilot.contracts import (
    ContractError,
    canonical_json,
    file_checksum,
    parse_record,
    semantic_hash,
    serialize_record,
    validate_case,
    validate_candidate,
    validate_evidence,
    validate_run,
)


def _hex(fill):
    return (fill * 64)[:64]


def _artifact(path='stock.stl', fill='a'):
    return {'path': path, 'sha256': _hex(fill)}


def _case():
    return {
        'schema_version': 'case-v1',
        'case_id': 'case-1',
        'version': 0,
        'family_id': 'family-1',
        'ancestor_id': None,
        'seed': 7,
        'units': 'mm',
        'coordinates': {
            'handedness': 'right',
            'up': 'Z',
            'top_z': 0,
            'tool_axis': [0, 0, 1],
        },
        'stock': {'L': 100.0, 'W': 50.0, 'H': 10.0},
        'target': {
            'kind': 'rounded_rect_blind_pocket',
            'a': 20.0,
            'b': 10.0,
            'R': 2.0,
            'd': 3.0,
        },
        'artifacts': {
            'stock': _artifact('stock.stl', 'a'),
            'target': _artifact('target.stl', 'b'),
        },
    }


def _candidate():
    return {
        'schema_version': 'candidate-v1',
        'candidate_id': 'candidate-1',
        'case_id': 'case-1',
        'version': 0,
        'case_hash': _hex('c'),
        'units': 'mm',
        'tools': [
            {'tool_id': 't1', 'radius': 3.0, 'cutting_length': 20.0, 'axis': [0, 0, 1]}
        ],
        'path': [
            {
                'start': [0.0, 0.0, 0.0],
                'end': [0.0, 0.0, -3.0],
                'tool_id': 't1',
                'operation_id': 'op',
                'mode': 'CUT_Z',
            },
            {
                'start': [0.0, 0.0, -3.0],
                'end': [10.0, 0.0, -3.0],
                'tool_id': 't1',
                'operation_id': 'op',
                'mode': 'CUT_XY',
            },
        ],
    }


def _evidence(check_id='Q3'):
    names = ('stock', 'target', 'tools', 'path', 'fixtures', 'coordinates', 'prior_state')
    keys = '1234567'
    dependencies = {name: _hex(keys[index]) for index, name in enumerate(names)}
    return {
        'schema_version': 'evidence-v1',
        'case_id': 'case-1',
        'candidate_id': 'candidate-1',
        'version': 0,
        'check_id': check_id,
        'status': 'PASS',
        'reason_code': 'OK',
        'metrics': {'margin': 0.5, 'samples': [1.0, 2.0]},
        'dependency_hashes': dependencies,
        'checker_version': 'checker-0.1.0',
        'tolerance_version': 'tolerance-1.0',
        'state_hash': _hex('d'),
        'wall_seconds': 0.25,
        'cpu_seconds': 0.5,
        'input_artifacts': [_artifact('input.bin', 'e')],
        'output_artifacts': [],
    }


def _run():
    return {
        'schema_version': 'run-v1',
        'run_id': 'run-1',
        'protocol_version': '1.0',
        'seed': 7,
        'case_hashes': {'case-1': _hex('a')},
        'candidate_hashes': {'candidate-1': _hex('b')},
        'config_hash': _hex('c'),
        'checker_version': 'checker-0.1.0',
        'tolerance_version': 'tolerance-1.0',
        'started_at': '2024-01-01T00:00:00Z',
        'mode': 'P1-A',
    }


def _expect(exc_info, status, reason_code):
    error = exc_info.value
    assert isinstance(error, ContractError)
    assert error.status == status
    assert error.reason_code == reason_code


@pytest.mark.parametrize(
    'kind,factory',
    [('case', _case), ('candidate', _candidate), ('evidence', _evidence), ('run', _run)],
)
def test_roundtrip_all_kinds(kind, factory):
    text = serialize_record(kind, factory())
    parsed = parse_record(kind, text)
    assert serialize_record(kind, parsed) == text
    assert parse_record(kind, text) == parsed


def test_case_normalizes_numbers():
    normalized = validate_case(_case())
    assert normalized['stock']['L'] == 100
    assert isinstance(normalized['stock']['L'], int)
    assert normalized['target']['R'] == 2


def test_case_coordinates_are_fixed():
    normalized = validate_case(_case())
    assert normalized['coordinates'] == {
        'handedness': 'right',
        'up': 'Z',
        'top_z': 0,
        'tool_axis': [0, 0, 1],
    }


def test_case_rejects_boolean_numbers():
    case = _case()
    case['version'] = True
    with pytest.raises(ContractError) as exc_info:
        validate_case(case)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_case_rejects_nonfinite_dimension():
    case = _case()
    case['stock']['L'] = float('inf')
    with pytest.raises(ContractError) as exc_info:
        validate_case(case)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


@pytest.mark.parametrize('field', ['L', 'W', 'H'])
def test_case_rejects_nonpositive_dimension(field):
    case = _case()
    case['stock'][field] = 0
    with pytest.raises(ContractError) as exc_info:
        validate_case(case)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_case_rejects_bad_units():
    case = _case()
    case['units'] = 'cm'
    with pytest.raises(ContractError) as exc_info:
        validate_case(case)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


@pytest.mark.parametrize('seed', [-1, 2 ** 64, 1.5, True])
def test_case_rejects_bad_seed(seed):
    case = _case()
    case['seed'] = seed
    with pytest.raises(ContractError) as exc_info:
        validate_case(case)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_case_rejects_unknown_schema_version():
    case = _case()
    case['schema_version'] = 'case-v2'
    with pytest.raises(ContractError) as exc_info:
        validate_case(case)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_case_rejects_extra_key():
    case = _case()
    case['expected_status'] = 'PASS'
    with pytest.raises(ContractError) as exc_info:
        validate_case(case)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_case_rejects_missing_key():
    case = _case()
    del case['seed']
    with pytest.raises(ContractError) as exc_info:
        validate_case(case)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_case_rejects_uncontained_target():
    case = _case()
    case['target']['a'] = 200.0
    with pytest.raises(ContractError) as exc_info:
        validate_case(case)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_case_rejects_too_large_radius():
    case = _case()
    case['target']['R'] = 6.0
    with pytest.raises(ContractError) as exc_info:
        validate_case(case)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_case_rejects_too_deep_target():
    case = _case()
    case['target']['d'] = 10.0
    with pytest.raises(ContractError) as exc_info:
        validate_case(case)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_case_rejects_negative_radius():
    case = _case()
    case['target']['R'] = -0.5
    with pytest.raises(ContractError) as exc_info:
        validate_case(case)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_case_coordinates_reject_tilted_axis():
    case = _case()
    case['coordinates']['tool_axis'] = [0, 1, 0]
    with pytest.raises(ContractError) as exc_info:
        validate_case(case)
    _expect(exc_info, 'UNKNOWN', 'UNKNOWN_UNSUPPORTED')


@pytest.mark.parametrize(
    'path',
    ['/etc/passwd', 'C:/tmp/x.stl', '//host/share/x', 'a/../b', '', 'a//b', 'a' + chr(92) + 'b'],
)
def test_case_rejects_bad_artifact_path(path):
    case = _case()
    case['artifacts']['stock']['path'] = path
    with pytest.raises(ContractError) as exc_info:
        validate_case(case)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_case_rejects_bad_checksum():
    case = _case()
    case['artifacts']['stock']['sha256'] = 'A' * 64
    with pytest.raises(ContractError) as exc_info:
        validate_case(case)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_candidate_requires_nonempty_tools():
    candidate = _candidate()
    candidate['tools'] = []
    with pytest.raises(ContractError) as exc_info:
        validate_candidate(candidate)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_candidate_rejects_duplicate_tool_ids():
    candidate = _candidate()
    candidate['tools'].append(
        {'tool_id': 't1', 'radius': 1.0, 'cutting_length': 5.0, 'axis': [0, 0, 1]}
    )
    with pytest.raises(ContractError) as exc_info:
        validate_candidate(candidate)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_candidate_rejects_bad_tool_reference():
    candidate = _candidate()
    candidate['path'][0]['tool_id'] = 'missing'
    with pytest.raises(ContractError) as exc_info:
        validate_candidate(candidate)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_candidate_rejects_discontinuous_path():
    candidate = _candidate()
    candidate['path'][1]['start'] = [5.0, 0.0, -3.0]
    with pytest.raises(ContractError) as exc_info:
        validate_candidate(candidate)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_candidate_rejects_xy_diagonal():
    candidate = _candidate()
    candidate['path'] = [
        {
            'start': [0, 0, -1],
            'end': [1, 0, -2],
            'tool_id': 't1',
            'operation_id': 'op',
            'mode': 'CUT_XY',
        }
    ]
    with pytest.raises(ContractError) as exc_info:
        validate_candidate(candidate)
    _expect(exc_info, 'UNKNOWN', 'UNKNOWN_UNSUPPORTED')


def test_candidate_rejects_z_diagonal():
    candidate = _candidate()
    candidate['path'] = [
        {
            'start': [0, 0, 0],
            'end': [1, 0, -1],
            'tool_id': 't1',
            'operation_id': 'op',
            'mode': 'CUT_Z',
        }
    ]
    with pytest.raises(ContractError) as exc_info:
        validate_candidate(candidate)
    _expect(exc_info, 'UNKNOWN', 'UNKNOWN_UNSUPPORTED')


def test_candidate_rejects_unknown_mode():
    candidate = _candidate()
    candidate['path'][0]['mode'] = 'CUT_ARC'
    with pytest.raises(ContractError) as exc_info:
        validate_candidate(candidate)
    _expect(exc_info, 'UNKNOWN', 'UNKNOWN_UNSUPPORTED')


def test_candidate_rejects_tilted_tool():
    candidate = _candidate()
    candidate['tools'][0]['axis'] = [0, 0, 2]
    with pytest.raises(ContractError) as exc_info:
        validate_candidate(candidate)
    _expect(exc_info, 'UNKNOWN', 'UNKNOWN_UNSUPPORTED')


def test_candidate_accepts_zero_length_segment():
    candidate = _candidate()
    candidate['path'] = [
        {
            'start': [1, 2, -3],
            'end': [1, 2, -3],
            'tool_id': 't1',
            'operation_id': 'op',
            'mode': 'CUT_XY',
        }
    ]
    normalized = validate_candidate(candidate)
    assert normalized['path'][0]['mode'] == 'CUT_XY'


def test_candidate_accepts_geometrically_bad_data():
    candidate = _candidate()
    candidate['tools'] = [
        {'tool_id': 't1', 'radius': 500.0, 'cutting_length': 1000.0, 'axis': [0, 0, 1]}
    ]
    candidate['path'] = [
        {
            'start': [0, 0, 0],
            'end': [0, 0, -500],
            'tool_id': 't1',
            'operation_id': 'op',
            'mode': 'CUT_Z',
        }
    ]
    normalized = validate_candidate(candidate)
    assert normalized['tools'][0]['radius'] == 500
    assert normalized['path'][0]['end'] == [0, 0, -500]


def test_candidate_rejects_private_label():
    candidate = _candidate()
    candidate['expected_status'] = 'FAIL'
    with pytest.raises(ContractError) as exc_info:
        validate_candidate(candidate)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_candidate_rejects_missing_keys():
    candidate = _candidate()
    del candidate['case_hash']
    with pytest.raises(ContractError) as exc_info:
        validate_candidate(candidate)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


@pytest.mark.parametrize(
    'status,reason_code',
    [
        ('PASS', 'OK'),
        ('FAIL', 'GEOMETRY_FAIL'),
        ('UNKNOWN', 'UNKNOWN_NUMERIC'),
        ('UNKNOWN', 'UNKNOWN_UNSUPPORTED'),
        ('UNKNOWN', 'UNKNOWN_TIMEOUT'),
        ('ERROR', 'ERROR_INPUT'),
        ('ERROR', 'ERROR_KERNEL'),
        ('ERROR', 'ERROR_PROCESS'),
    ],
)
def test_evidence_accepts_status_reason_pairs(status, reason_code):
    evidence = _evidence()
    evidence['status'] = status
    evidence['reason_code'] = reason_code
    assert validate_evidence(evidence)['reason_code'] == reason_code


def test_evidence_rejects_mismatched_status_reason():
    evidence = _evidence()
    evidence['status'] = 'PASS'
    evidence['reason_code'] = 'GEOMETRY_FAIL'
    with pytest.raises(ContractError) as exc_info:
        validate_evidence(evidence)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_evidence_geometry_fail_is_not_error():
    evidence = _evidence()
    evidence['status'] = 'FAIL'
    evidence['reason_code'] = 'GEOMETRY_FAIL'
    assert validate_evidence(evidence)['status'] == 'FAIL'
    bad = _evidence()
    bad['status'] = 'ERROR'
    bad['reason_code'] = 'GEOMETRY_FAIL'
    with pytest.raises(ContractError):
        validate_evidence(bad)


def test_evidence_requires_check_dependencies():
    evidence = _evidence('Q3')
    del evidence['dependency_hashes']['target']
    with pytest.raises(ContractError) as exc_info:
        validate_evidence(evidence)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_evidence_rejects_empty_dependencies():
    evidence = _evidence()
    evidence['dependency_hashes'] = {}
    with pytest.raises(ContractError) as exc_info:
        validate_evidence(evidence)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_evidence_accepts_q1_and_q2_dependencies():
    for check_id in ('Q1', 'Q2'):
        evidence = _evidence(check_id)
        assert validate_evidence(evidence)['check_id'] == check_id


def test_evidence_q1_requires_fixtures():
    evidence = _evidence('Q1')
    del evidence['dependency_hashes']['fixtures']
    with pytest.raises(ContractError) as exc_info:
        validate_evidence(evidence)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_evidence_q2_requires_prior_state():
    evidence = _evidence('Q2')
    del evidence['dependency_hashes']['prior_state']
    with pytest.raises(ContractError) as exc_info:
        validate_evidence(evidence)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


@pytest.mark.parametrize('value', [True, '0.5', None, {'nested': 1}, [[1.0]]])
def test_evidence_rejects_bad_metric_values(value):
    evidence = _evidence()
    evidence['metrics'] = {'m': value}
    with pytest.raises(ContractError) as exc_info:
        validate_evidence(evidence)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_evidence_rejects_nonfinite_metric():
    evidence = _evidence()
    evidence['metrics'] = {'m': float('nan')}
    with pytest.raises(ContractError) as exc_info:
        validate_evidence(evidence)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_evidence_requires_versions():
    evidence = _evidence()
    evidence['checker_version'] = ''
    with pytest.raises(ContractError) as exc_info:
        validate_evidence(evidence)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_run_requires_utc_timestamp():
    run = _run()
    run['started_at'] = '2024-01-01T00:00:00'
    with pytest.raises(ContractError) as exc_info:
        validate_run(run)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_run_rejects_non_utc_offset():
    run = _run()
    run['started_at'] = '2024-01-01T00:00:00+05:00'
    with pytest.raises(ContractError) as exc_info:
        validate_run(run)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_run_normalizes_utc_timestamp():
    run = _run()
    run['started_at'] = '2024-01-01T00:00:00+00:00'
    assert validate_run(run)['started_at'] == '2024-01-01T00:00:00Z'


def test_run_rejects_empty_hash_maps():
    run = _run()
    run['case_hashes'] = {}
    with pytest.raises(ContractError) as exc_info:
        validate_run(run)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_run_rejects_bad_mode():
    run = _run()
    run['mode'] = 'P2'
    with pytest.raises(ContractError) as exc_info:
        validate_run(run)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_run_rejects_non_uint64_seed():
    run = _run()
    run['seed'] = 2 ** 64
    with pytest.raises(ContractError) as exc_info:
        validate_run(run)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_run_rejects_private_files():
    run = _run()
    run['evaluator'] = 'private.txt'
    with pytest.raises(ContractError) as exc_info:
        validate_run(run)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_canonical_json_is_sorted_and_compact():
    assert canonical_json({'b': 2, 'a': 1}) == '{"a":1,"b":2}'


def test_canonical_json_normalizes_equal_numbers():
    assert canonical_json({'v': 1}) == canonical_json({'v': 1.0})
    assert canonical_json({'v': -0.0}) == canonical_json({'v': 0})
    assert canonical_json({'v': True}) == '{"v":true}'
    assert canonical_json({'v': True}) != canonical_json({'v': 1})


def test_canonical_json_preserves_large_integers():
    text = canonical_json({'v': 2 ** 200})
    assert str(2 ** 200) in text


def test_canonical_json_rejects_nonfinite_and_non_string_keys():
    with pytest.raises(ContractError):
        canonical_json({'v': float('nan')})
    with pytest.raises(ContractError):
        canonical_json({1: 'a'})


def test_semantic_hash_is_order_invariant():
    assert semantic_hash({'a': 1, 'b': 2}) == semantic_hash({'b': 2, 'a': 1})


def test_semantic_hash_normalizes_equal_numbers():
    assert semantic_hash({'v': 1}) == semantic_hash({'v': 1.0})
    assert semantic_hash({'v': -0.0}) == semantic_hash({'v': 0})
    assert semantic_hash({'v': True}) != semantic_hash({'v': 1})


def test_semantic_hash_keeps_large_integer_precision():
    assert semantic_hash({'v': 2 ** 200}) != semantic_hash({'v': 2 ** 200 + 1})


def test_semantic_hash_notices_tiny_real_changes():
    assert semantic_hash({'v': 0.1}) != semantic_hash({'v': 0.1000000000000001})


def test_semantic_hash_includes_versions_and_dependency_values():
    base = {'checker_version': '1', 'tolerance_version': '1', 'deps': {'stock': 'a' * 64}}
    changed_version = {
        'checker_version': '2',
        'tolerance_version': '1',
        'deps': {'stock': 'a' * 64},
    }
    changed_dependency = {
        'checker_version': '1',
        'tolerance_version': '1',
        'deps': {'stock': 'b' * 64},
    }
    assert semantic_hash(base) != semantic_hash(changed_version)
    assert semantic_hash(base) != semantic_hash(changed_dependency)


def test_semantic_hash_does_not_drop_unknown_fields():
    assert semantic_hash({'known': 1}) != semantic_hash({'known': 1, 'extra': 2})


def test_file_checksum_differs_from_semantic_hash(tmp_path):
    first = tmp_path / 'first.json'
    second = tmp_path / 'second.json'
    first.write_text('{"a":1,"b":2}', encoding='utf-8')
    second.write_text('{ "b": 2,   "a": 1 }', encoding='utf-8')
    assert file_checksum(first) != file_checksum(second)
    assert semantic_hash(json.loads(first.read_text(encoding='utf-8'))) == semantic_hash(
        json.loads(second.read_text(encoding='utf-8'))
    )


def test_parse_record_rejects_duplicate_keys():
    text = '{"schema_version":"run-v1","run_id":"a","run_id":"b"}'
    with pytest.raises(ContractError) as exc_info:
        parse_record('run', text)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_parse_record_rejects_nan():
    text = '{"schema_version":"run-v1","seed":NaN}'
    with pytest.raises(ContractError) as exc_info:
        parse_record('run', text)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_parse_record_rejects_wrong_kind():
    text = serialize_record('run', _run())
    with pytest.raises(ContractError) as exc_info:
        parse_record('case', text)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_parse_record_rejects_unknown_schema_version():
    text = '{"schema_version":"run-v2"}'
    with pytest.raises(ContractError) as exc_info:
        parse_record('run', text)
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_serialize_record_rejects_unknown_kind():
    with pytest.raises(ContractError) as exc_info:
        serialize_record('plan', {})
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_parse_record_rejects_non_object():
    with pytest.raises(ContractError) as exc_info:
        parse_record('run', '[]')
    _expect(exc_info, 'ERROR', 'ERROR_INPUT')


def test_returned_records_are_independent():
    case = _case()
    snapshot = copy.deepcopy(case)
    normalized = validate_case(case)
    normalized['stock']['L'] = 1
    assert case == snapshot
    case['stock']['W'] = 999
    assert normalized['stock']['W'] == 50
    parsed_first = parse_record('candidate', serialize_record('candidate', _candidate()))
    parsed_second = parse_record('candidate', serialize_record('candidate', _candidate()))
    parsed_first['tools'][0]['radius'] = 99
    assert parsed_second['tools'][0]['radius'] == 3

# --- reviewed step2 contract edge cases ---

_HEX64 = 'a' * 64


def _case_with_target(**overrides):
    case = dict(_case())
    target = dict(case['target'])
    target.update(overrides)
    case['target'] = target
    return case


def _case_with_stock_artifact(**overrides):
    case = dict(_case())
    artifacts = dict(case['artifacts'])
    stock = dict(artifacts['stock'])
    stock.update(overrides)
    artifacts['stock'] = stock
    case['artifacts'] = artifacts
    return case


@pytest.mark.parametrize('suffix', ['\n', '\r\n'])
def test_case_artifact_sha256_rejects_trailing_newline(suffix):
    case = _case_with_stock_artifact(sha256=_HEX64 + suffix)
    _expect_rejection('ERROR_INPUT', lambda: validate_case(case))


@pytest.mark.parametrize('suffix', ['\n', '\r\n'])
def test_candidate_case_hash_rejects_trailing_newline(suffix):
    candidate = dict(_candidate())
    candidate['case_hash'] = _HEX64 + suffix
    _expect_rejection('ERROR_INPUT', lambda: validate_candidate(candidate))


@pytest.mark.parametrize('suffix', ['\n', '\r\n'])
def test_evidence_dependency_hash_rejects_trailing_newline(suffix):
    evidence = dict(_evidence())
    dependency_hashes = dict(evidence['dependency_hashes'])
    dependency_hashes[next(iter(dependency_hashes))] = _HEX64 + suffix
    evidence['dependency_hashes'] = dependency_hashes
    _expect_rejection('ERROR_INPUT', lambda: validate_evidence(evidence))


@pytest.mark.parametrize(
    'bad_path',
    [
        'dir\x00/file.stl',
        'dir/file\n.stl',
        'dir/file\t.stl',
        '\x01file.stl',
        '\x1ffile.stl',
        '\x7ffile.stl',
    ],
)
def test_portable_path_rejects_ascii_control_characters(bad_path):
    case = _case_with_stock_artifact(path=bad_path)
    _expect_rejection('ERROR_INPUT', lambda: validate_case(case))


@pytest.mark.parametrize(
    'good_path',
    ['models/part.stl', '零件/刀具.stl', 'a b/c-d_e.stl'],
)
def test_portable_path_accepts_ordinary_and_unicode_paths(good_path):
    case = _case_with_stock_artifact(path=good_path)
    validated = validate_case(case)
    assert validated['artifacts']['stock']['path'] == good_path


@pytest.mark.parametrize(
    'mode,reason,status',
    [
        (None, 'ERROR_INPUT', 'ERROR'),
        (True, 'ERROR_INPUT', 'ERROR'),
        (0, 'ERROR_INPUT', 'ERROR'),
        ([], 'ERROR_INPUT', 'ERROR'),
        ('CUT_ARC', 'UNKNOWN_UNSUPPORTED', 'UNKNOWN'),
    ],
)
def test_candidate_motion_mode_type_and_support(mode, reason, status):
    candidate = dict(_candidate())
    path = [dict(segment) for segment in candidate['path']]
    path[0]['mode'] = mode
    candidate['path'] = path
    _expect_rejection(reason, lambda: validate_candidate(candidate))
    with pytest.raises(ContractError) as info:
        validate_candidate(candidate)
    assert info.value.status == status


def test_case_target_rejects_zero_radius():
    _expect_rejection('ERROR_INPUT', lambda: validate_case(_case_with_target(R=0)))


@pytest.mark.parametrize('axis', ['a', 'b'])
def test_case_target_rejects_touching_stock_sidewalls(axis):
    case = dict(_case())
    target = dict(case['target'])
    target[axis] = case['stock']['L' if axis == 'a' else 'W']
    case['target'] = target
    _expect_rejection('ERROR_INPUT', lambda: validate_case(case))


def test_case_target_radius_half_min_remains_valid():
    case = dict(_case())
    target = dict(case['target'])
    target['R'] = min(target['a'], target['b']) / 2
    case['target'] = target
    validated = validate_case(case)
    assert validated['target']['R'] == target['R']

def _expect_rejection(reason_code, action):
    with pytest.raises(ContractError) as exc_info:
        action()
    status = {"ERROR_INPUT": "ERROR"}.get(reason_code, "UNKNOWN")
    _expect(exc_info, status, reason_code)


@pytest.mark.parametrize(
    ("mode", "end"),
    [
        ("CUT_XY", [1.0, 2.0, 0.0]),
        ("CUT_Z", [0.0, 0.0, -2.0]),
        ("RAPID_XY", [1.0, 2.0, 0.0]),
        ("RAPID_Z", [0.0, 0.0, -2.0]),
    ],
)
def test_validate_candidate_preserves_motion_mode(mode, end):
    candidate = _candidate()
    candidate["path"] = [
        {
            "start": [0.0, 0.0, 0.0],
            "end": end,
            "tool_id": "t1",
            "operation_id": "op",
            "mode": mode,
        }
    ]

    validated = validate_candidate(candidate)

    assert validated["path"][0]["mode"] == mode
