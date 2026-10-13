'''Focused tests for pilot.targets (G0 target design generation).'''

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from pilot import targets
from pilot.contracts import ContractError, file_checksum, semantic_hash, validate_case

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / 'configs' / 'g0_targets_v1.json'

EXPECTED_TABLE = {
    'S01': ((60, 50, 16), (30, 24, 3, 6)),
    'S02': ((80, 45, 16), (54, 14, 3, 5)),
    'S03': ((60, 55, 20), (36, 30, 4, 12)),
    'S04': ((70, 55, 16), (46, 32, 2, 4)),
    'S05': ((60, 50, 12), (48, 36, 3, 3)),
    'S06': ((55, 50, 20), (24, 20, 4, 10)),
}

TINY_CASE = {
    'case_id': 'T01',
    'family_id': 'F1',
    'ancestor_id': None,
    'stock': {'L': 24, 'W': 18, 'H': 10},
    'target': {'kind': 'rounded_rect_blind_pocket', 'a': 10, 'b': 8, 'R': 2, 'd': 4},
}


@pytest.fixture(scope='module')
def tiny_run(tmp_path_factory):
    root = tmp_path_factory.mktemp('tiny')
    result = targets.generate_case(TINY_CASE, root, targets.FROZEN_MASTER_SEED, preview=True)
    return {'root': root, 'result': result}


@pytest.fixture(scope='module')
def s01_run(tmp_path_factory):
    root = tmp_path_factory.mktemp('s01')
    spec = targets.FROZEN_CASES[0]
    result = targets.generate_case(spec, root, targets.FROZEN_MASTER_SEED, preview=False)
    return {'root': root, 'result': result}


def _case_stub():
    return {
        'schema_version': 'case-v1',
        'case_id': 'X01',
        'version': 0,
        'family_id': 'F1',
        'ancestor_id': None,
        'seed': 1,
        'units': 'mm',
        'coordinates': {'handedness': 'right', 'up': 'Z', 'top_z': 0, 'tool_axis': [0, 0, 1]},
        'stock': {'L': 40.0, 'W': 30.0, 'H': 12.0},
        'target': {'kind': 'rounded_rect_blind_pocket', 'a': 20.0, 'b': 10.0, 'R': 3.0, 'd': 4.0},
        'artifacts': {
            'stock': {'path': 'X01/stock.step', 'sha256': 'a' * 64},
            'target': {'path': 'X01/target.step', 'sha256': 'b' * 64},
        },
    }


# --- frozen protocol --------------------------------------------------------


def test_frozen_config_matches_table():
    data = targets.load_config(CONFIG_PATH)
    assert data['config_version'] == 'g0-targets-v1'
    assert data['family_id'] == 'F1'
    assert data['master_seed'] == 20261012
    assert len(data['cases']) == 6
    for row in data['cases']:
        stock, target = EXPECTED_TABLE[row['case_id']]
        assert row['family_id'] == 'F1'
        assert (row['stock']['L'], row['stock']['W'], row['stock']['H']) == stock
        assert (row['target']['a'], row['target']['b'], row['target']['R'], row['target']['d']) == target
    assert sorted(EXPECTED_TABLE) == sorted(row['case_id'] for row in data['cases'])


def test_frozen_table_constants_match_module():
    for row in targets.FROZEN_CASES:
        stock, target = EXPECTED_TABLE[row['case_id']]
        assert (row['stock']['L'], row['stock']['W'], row['stock']['H']) == stock
        assert (row['target']['a'], row['target']['b'], row['target']['R'], row['target']['d']) == target


def test_seed_rule_matches_sha256_big_endian():
    for case_id in EXPECTED_TABLE:
        payload = '20261012|F1|{0}|G0_TARGET'.format(case_id).encode('utf-8')
        expected = int.from_bytes(hashlib.sha256(payload).digest()[:8], 'big')
        assert targets.derive_seed(20261012, 'F1', case_id) == expected
        assert 0 <= expected < 2 ** 64


@pytest.mark.parametrize('mutate', [
    lambda data: data['cases'][2]['target'].update(a=44),
    lambda data: data.update(master_seed=1),
    lambda data: data.update(family_id='F2'),
    lambda data: data['cases'].pop(),
    lambda data: data['tolerances'].update(bbox_mm=0.01),
])
def test_frozen_config_tampering_rejected(tmp_path, mutate):
    data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    mutate(data)
    tampered = tmp_path / 'tampered.json'
    tampered.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(targets.TargetError):
        targets.load_config(tampered)


# --- contract validation ----------------------------------------------------


def test_valid_case_stub_is_accepted():
    record = validate_case(_case_stub())
    assert record['case_id'] == 'X01'
    assert record['units'] == 'mm'


@pytest.mark.parametrize('mutate', [
    lambda record: record['target'].update(a=0),
    lambda record: record['target'].update(a=40.0),
    lambda record: record['target'].update(R=6.0),
    lambda record: record['target'].update(d=0),
    lambda record: record['target'].update(d=12.0),
    lambda record: record['target'].update(kind='slot'),
    lambda record: record.update(units='cm'),
    lambda record: record.update(unexpected=1),
    lambda record: record['artifacts']['stock'].update(sha256='A' * 64),
])
def test_validate_case_rejects_invalid_input(mutate):
    record = _case_stub()
    mutate(record)
    with pytest.raises(ContractError):
        validate_case(record)


# --- S01 end to end ---------------------------------------------------------


def test_s01_case_record_and_hashes(s01_run):
    root = s01_run['root']
    result = s01_run['result']
    assert result['status'] == 'PASS', result['error']
    case = json.loads((root / 'S01' / 'case.json').read_text(encoding='utf-8'))
    validate_case(case)
    assert case['case_id'] == 'S01'
    assert case['family_id'] == 'F1'
    assert case['ancestor_id'] is None
    assert case['seed'] == targets.derive_seed(20261012, 'F1', 'S01')
    assert case['artifacts']['stock']['path'] == 'S01/stock.step'
    assert case['artifacts']['target']['path'] == 'S01/target.step'
    for name in ('stock', 'target'):
        artifact = root / case['artifacts'][name]['path']
        assert artifact.is_file()
        assert artifact.stat().st_size > 0
        assert file_checksum(artifact) == case['artifacts'][name]['sha256']
    assert result['design_semantic_hash'] != result['case_record_hash']


def test_s01_validation_report_numbers(s01_run):
    report = json.loads((s01_run['root'] / 'S01' / 'validation.json').read_text(encoding='utf-8'))
    assert report['schema_version'] == 'target-validation-v1'
    assert report['scope'] == targets.VALIDATION_SCOPE
    assert report['status'] == 'PASS'
    names = {check['name'] for check in report['checks']}
    for name in (
        'preexport.target_floor_plane',
        'preexport.target_floor_footprint',
        'preexport.target_wall_bottom_thickness',
        'preexport.target_cylinder_radii',
        'preexport.volume_conservation',
        'reimport.target_floor_plane',
        'reimport.target_cylinder_radii',
        'target.reimport_volume',
    ):
        assert name in names
    floor = next(check for check in report['checks'] if check['name'] == 'preexport.target_floor_plane')
    assert floor['pass']
    assert abs(floor['observed']['floor_z_mm'] + 6.0) <= 1e-3
    footprint = next(check for check in report['checks'] if check['name'] == 'preexport.target_floor_footprint')
    assert footprint['pass']
    assert abs(footprint['observed']['a_mm'] - 30.0) <= 1e-3
    assert abs(footprint['observed']['b_mm'] - 24.0) <= 1e-3
    wall = next(check for check in report['checks'] if check['name'] == 'preexport.target_wall_bottom_thickness')
    assert wall['pass']
    assert wall['observed']['bottom_mm'] > 0
    assert wall['observed']['left_mm'] > 0
    cylinders = next(check for check in report['checks'] if check['name'] == 'preexport.target_cylinder_radii')
    assert cylinders['pass'] and cylinders['observed']['count'] >= 1
    assert all(abs(radius - 3.0) <= 1e-3 for radius in cylinders['observed']['radii'])
    assert all(cylinders['observed']['vertical'])
    volume = next(check for check in report['checks'] if check['name'] == 'preexport.target_volume')
    analytic = 60 * 50 * 16 - targets.analytic_pocket_volume(30.0, 24.0, 3.0, 6.0)
    assert abs(volume['observed']['target_volume_mm3'] - analytic) <= 1e-3
    assert 'nan' not in json.dumps(report).lower()


def test_s01_step_reimport_matches_preexport(s01_run):
    root = s01_run['root']
    roots_stock = targets.reimport_step(root / 'S01' / 'stock.step')
    roots_target = targets.reimport_step(root / 'S01' / 'target.step')
    stock = targets.measure_roots(roots_stock)
    target = targets.measure_roots(roots_target)
    assert stock['solid_count'] == 1 and stock['valid'] and stock['finite']
    assert target['solid_count'] == 1 and target['valid'] and target['finite']
    assert targets._bbox_close(stock['bbox'], {'xmin': 0.0, 'xmax': 60.0, 'ymin': 0.0, 'ymax': 50.0, 'zmin': -16.0, 'zmax': 0.0}, 0.001)
    assert targets._bbox_close(target['bbox'], {'xmin': 0.0, 'xmax': 60.0, 'ymin': 0.0, 'ymax': 50.0, 'zmin': -16.0, 'zmax': 0.0}, 0.001)
    analytic = 60 * 50 * 16 - targets.analytic_pocket_volume(30.0, 24.0, 3.0, 6.0)
    assert abs(target['volume'] - analytic) <= targets.volume_tolerance(analytic)


# --- generic builder --------------------------------------------------------


def test_generic_builder_supports_full_circle_radius():
    spec = {
        'stock': {'L': 40.0, 'W': 30.0, 'H': 10.0},
        'target': {'kind': 'rounded_rect_blind_pocket', 'a': 20.0, 'b': 20.0, 'R': 10.0, 'd': 5.0},
    }
    built = targets.build_design(spec)
    measured = targets.measure_roots([built['target']])
    assert measured['solid_count'] == 1 and measured['valid'] and measured['finite']
    pocket_volume = targets.analytic_pocket_volume(20.0, 20.0, 10.0, 5.0)
    expected = 40.0 * 30.0 * 10.0 - pocket_volume
    assert abs(measured['volume'] - expected) <= targets.volume_tolerance(expected)
    cylinders = targets.cylinder_faces(measured['faces'])
    assert len(cylinders) >= 1
    assert all(abs(item['radius'] - 10.0) <= 1e-3 for item in cylinders)
    assert all(item['vertical'] for item in cylinders)


def test_generic_builder_supports_capsule_radius():
    spec = {
        'stock': {'L': 40.0, 'W': 30.0, 'H': 10.0},
        'target': {'kind': 'rounded_rect_blind_pocket', 'a': 24.0, 'b': 12.0, 'R': 6.0, 'd': 4.0},
    }
    built = targets.build_design(spec)
    measured = targets.measure_roots([built['target']])
    assert measured['solid_count'] == 1 and measured['valid'] and measured['finite']
    pocket_volume = targets.analytic_pocket_volume(24.0, 12.0, 6.0, 4.0)
    expected = 40.0 * 30.0 * 10.0 - pocket_volume
    assert abs(measured['volume'] - expected) <= targets.volume_tolerance(expected)
    cylinders = targets.cylinder_faces(measured['faces'])
    assert len(cylinders) >= 1
    assert all(abs(item['radius'] - 6.0) <= 1e-3 for item in cylinders)


def test_build_design_rejects_out_of_contract_radius():
    with pytest.raises(targets.TargetError):
        targets.build_design({
            'stock': {'L': 40.0, 'W': 30.0, 'H': 10.0},
            'target': {'kind': 'rounded_rect_blind_pocket', 'a': 20.0, 'b': 10.0, 'R': 6.0, 'd': 4.0},
        })


# --- hashes -----------------------------------------------------------------


def test_design_hash_invariant_and_provenance_distinct(tmp_path_factory):
    root_a = tmp_path_factory.mktemp('hash_a')
    root_b = tmp_path_factory.mktemp('hash_b')
    first = targets.generate_case(targets.FROZEN_CASES[0], root_a, 20261012, preview=False)
    second = targets.generate_case(targets.FROZEN_CASES[0], root_b, 999, preview=False)
    assert first['status'] == 'PASS', first['error']
    assert second['status'] == 'PASS', second['error']
    assert first['design_semantic_hash'] == second['design_semantic_hash']
    assert first['design_semantic_hash'] != first['case_record_hash']
    case = json.loads((root_a / 'S01' / 'case.json').read_text(encoding='utf-8'))
    expected = semantic_hash({
        'schema_version': 'design-v1',
        'protocol_version': '1.0',
        'units': case['units'],
        'coordinates': case['coordinates'],
        'stock': case['stock'],
        'target': case['target'],
    })
    assert first['design_semantic_hash'] == expected


def test_design_hash_changes_with_design_parameters(tmp_path):
    base = targets.generate_case(TINY_CASE, tmp_path / 'base', 1, preview=False)
    variant_spec = json.loads(json.dumps(TINY_CASE))
    variant_spec['case_id'] = 'T02'
    variant_spec['target']['a'] = 12
    variant = targets.generate_case(variant_spec, tmp_path / 'variant', 1, preview=False)
    assert base['status'] == 'PASS' and variant['status'] == 'PASS'
    assert base['design_semantic_hash'] != variant['design_semantic_hash']


# --- persistence hygiene ----------------------------------------------------


def test_persisted_paths_relative_and_no_placeholder_hashes(tiny_run):
    root = tiny_run['root']
    result = tiny_run['result']
    assert result['status'] == 'PASS', result['error']
    assert result['case_path'] == 'T01/case.json'
    assert result['validation_path'] == 'T01/validation.json'
    case_path = root / 'T01' / 'case.json'
    validation_path = root / 'T01' / 'validation.json'
    assert case_path.is_file()
    assert validation_path.is_file()
    assert not (root / 'manifest.json').exists()
    text = ''
    for path in sorted(root.rglob('*.json')):
        text += path.read_text(encoding='utf-8')
    assert str(root) not in text
    assert '/Users/' not in text
    assert '\\' not in text
    assert '0' * 64 not in text
    case = json.loads(case_path.read_text(encoding='utf-8'))
    validate_case(case)
    assert case['case_id'] == 'T01'
    for name in ('stock', 'target'):
        artifact = case['artifacts'][name]
        assert artifact['path'] == 'T01/{0}.step'.format(name)
        artifact_path = root / artifact['path']
        assert artifact_path.is_file()
        assert file_checksum(artifact_path) == artifact['sha256']
        assert result['artifacts'][name]['sha256'] == artifact['sha256']


def test_preview_png_is_a_real_png(tiny_run):
    png = tiny_run['root'] / 'T01' / 'preview.png'
    assert png.is_file()
    assert png.stat().st_size > 1000
    assert png.read_bytes()[:8] == b'\x89PNG\r\n\x1a\n'


# --- failure handling -------------------------------------------------------


def test_output_overwrite_rejected(tmp_path):
    spec = json.loads(json.dumps(TINY_CASE))
    spec['case_id'] = 'T99'
    targets.generate_case(spec, tmp_path, 1, preview=False)
    with pytest.raises(targets.TargetError):
        targets.generate_case(spec, tmp_path, 1, preview=False)


def test_injected_export_failure_never_passes(monkeypatch, tmp_path):
    def boom(shape, path):
        raise RuntimeError('injected export failure')
    monkeypatch.setattr(targets, 'export_shape', boom)
    spec = json.loads(json.dumps(TINY_CASE))
    spec['case_id'] = 'E01'
    result = targets.generate_case(spec, tmp_path, 1, preview=False)
    assert result['status'] == 'ERROR'
    assert result['reason_code'] == 'ERROR'
    assert 'export' in (result['error'] or '').lower()
    assert (tmp_path / 'E01' / 'validation.json').is_file()
    assert not (tmp_path / 'E01' / 'case.json').exists()


def test_injected_import_failure_never_passes(monkeypatch, tmp_path):
    def boom(path):
        raise RuntimeError('injected import failure')
    monkeypatch.setattr(targets, 'reimport_step', boom)
    spec = json.loads(json.dumps(TINY_CASE))
    spec['case_id'] = 'E02'
    result = targets.generate_case(spec, tmp_path, 1, preview=False)
    assert result['status'] == 'ERROR'
    assert result['reason_code'] == 'ERROR'
    assert 'import' in (result['error'] or '').lower()
    assert not (tmp_path / 'E02' / 'case.json').exists()
    assert (tmp_path / 'E02' / 'validation.json').is_file()


# --- batch ------------------------------------------------------------------


def _batch_config(case_ids):
    cases = []
    for case_id in case_ids:
        row = json.loads(json.dumps(TINY_CASE))
        row['case_id'] = case_id
        cases.append(row)
    return {
        'config_version': 'test-batch-v1',
        'protocol_version': '1.0',
        'family_id': 'F1',
        'master_seed': 20261012,
        'units': 'mm',
        'coordinates': dict(targets.FROZEN_COORDINATES),
        'seed_rule': targets.FROZEN_SEED_RULE,
        'tolerances': dict(targets.FROZEN_TOLERANCES),
        'cases': cases,
    }


def test_batch_all_pass_writes_manifest(tmp_path):
    config = _batch_config(['P01'])
    result = targets.generate_batch(config, tmp_path, master_seed=1, preview=False)
    assert result['status'] == 'PASS'
    manifest = json.loads((tmp_path / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'PASS'
    assert (tmp_path / 'config_snapshot.json').is_file()
    assert manifest['cases'][0]['validation_path'] == 'P01/validation.json'


def test_batch_partial_failure_preserves_row(tmp_path):
    config = _batch_config(['B01', 'B02'])
    config['cases'][1]['target']['a'] = 30.0
    result = targets.generate_batch(config, tmp_path, master_seed=1, preview=False)
    assert result['status'] == 'ERROR'
    manifest = json.loads((tmp_path / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'ERROR'
    assert manifest['error']
    assert [row['case_id'] for row in manifest['cases']] == ['B01', 'B02']
    statuses = {row['case_id']: row['status'] for row in manifest['cases']}
    assert statuses['B01'] == 'PASS'
    assert statuses['B02'] == 'ERROR'
    assert manifest['cases'][1]['reason_code'] == 'ERROR'
    assert (tmp_path / 'B01' / 'case.json').is_file()
    assert not (tmp_path / 'B02' / 'case.json').exists()
    assert (tmp_path / 'B02' / 'validation.json').is_file()


# --- CLI --------------------------------------------------------------------


def test_main_failure_exit_code_for_missing_config(tmp_path, capsys):
    code = targets.main(['--config', str(tmp_path / 'missing.json'), '--case', 'S01', '--out', str(tmp_path / 'out')])
    assert code != 0
    assert not (tmp_path / 'out').exists()
    assert 'error' in capsys.readouterr().err.lower()


def test_main_failure_exit_code_for_tampered_config(tmp_path, capsys):
    data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    data['cases'][0]['target']['a'] = 31
    tampered = tmp_path / 'tampered.json'
    tampered.write_text(json.dumps(data), encoding='utf-8')
    code = targets.main(['--config', str(tampered), '--case', 'S01', '--out', str(tmp_path / 'out')])
    assert code != 0
    assert not (tmp_path / 'out').exists()


def test_main_single_case_success_with_previews(tmp_path):
    root = tmp_path / 'run'
    code = targets.main(['--config', str(CONFIG_PATH), '--case', 'S01', '--out', str(root)])
    assert code == 0
    assert (root / 'S01' / 'case.json').is_file()
    assert (root / 'S01' / 'validation.json').is_file()
    assert (root / 'S01' / 'preview.png').is_file()
    assert (root / 'overview.png').is_file()
    assert (root / 'manifest.json').is_file()
    assert (root / 'config_snapshot.json').is_file()
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'PASS'
    assert manifest['cases'][0]['case_id'] == 'S01'


def test_main_unknown_case_exit_code(tmp_path, capsys):
    code = targets.main(['--config', str(CONFIG_PATH), '--case', 'S99', '--out', str(tmp_path / 'run')])
    assert code != 0
    assert 'S99' in capsys.readouterr().err




# --- appended strict-boundary and error-semantics tests ---------------------


def test_config_rejects_unknown_root_and_nested_keys(tmp_path):
    data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    data['unexpected'] = 1
    extra_root = tmp_path / 'extra_root.json'
    extra_root.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(targets.TargetError):
        targets.load_config(extra_root)

    data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    data['cases'][0]['stock']['Lx'] = 1
    extra_nested = tmp_path / 'extra_nested.json'
    extra_nested.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(targets.TargetError):
        targets.load_config(extra_nested)


def test_build_design_rejects_unknown_root_and_nested_keys():
    base = {
        'stock': {'L': 24.0, 'W': 18.0, 'H': 10.0},
        'target': {'kind': 'rounded_rect_blind_pocket', 'a': 10.0, 'b': 8.0, 'R': 2.0, 'd': 4.0},
    }
    with pytest.raises(targets.TargetError):
        targets.build_design({**base, 'unexpected': 1})
    nested_stock = json.loads(json.dumps(base))
    nested_stock['stock']['units'] = 'mm'
    with pytest.raises(targets.TargetError):
        targets.build_design(nested_stock)
    nested_target = json.loads(json.dumps(base))
    nested_target['target']['tool'] = 'flat'
    with pytest.raises(targets.TargetError):
        targets.build_design(nested_target)


def test_generate_case_rejects_unknown_spec_keys(tmp_path):
    spec = json.loads(json.dumps(TINY_CASE))
    spec['unexpected'] = 1
    with pytest.raises(targets.TargetError):
        targets.generate_case(spec, tmp_path / 'out', 1, preview=False)
    assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('bad', [True, '10', '10.0', None, [10]])
def test_build_design_rejects_non_numeric_dimensions(bad):
    spec = json.loads(json.dumps(TINY_CASE))
    spec['target']['a'] = bad
    with pytest.raises(targets.TargetError):
        targets.build_design(spec)


@pytest.mark.parametrize('seed', [20261012.0, True, '20261012', -1, 2 ** 64])
def test_master_seed_must_be_uint64_int(seed):
    with pytest.raises(targets.TargetError):
        targets.derive_seed(seed, 'F1', 'S01')


@pytest.mark.parametrize('seed', [20261012.0, True])
def test_frozen_config_rejects_non_int_master_seed(tmp_path, seed):
    data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    data['master_seed'] = seed
    path = tmp_path / 'seed.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(targets.TargetError):
        targets.load_config(path)


def test_case_id_traversal_cannot_escape_output_root(tmp_path):
    root = tmp_path / 'out'
    spec = json.loads(json.dumps(TINY_CASE))
    spec['case_id'] = '../escape'
    with pytest.raises(targets.TargetError):
        targets.generate_case(spec, root, 1, preview=False)
    assert not (tmp_path / 'escape').exists()
    assert not root.exists()


@pytest.mark.parametrize('bad_id', ['/abs', 'a/b', 'a\\b', '..', 123, ''])
def test_generate_case_rejects_unsafe_case_id(tmp_path, bad_id):
    spec = json.loads(json.dumps(TINY_CASE))
    spec['case_id'] = bad_id
    with pytest.raises(targets.TargetError):
        targets.generate_case(spec, tmp_path / 'out', 1, preview=False)


def test_radius_above_half_min_rejected():
    spec = {
        'stock': {'L': 24.0, 'W': 18.0, 'H': 10.0},
        'target': {'kind': 'rounded_rect_blind_pocket', 'a': 10.0, 'b': 8.0, 'R': 4.0 + 1e-10, 'd': 4.0},
    }
    with pytest.raises(targets.TargetError):
        targets.build_design(spec)


def test_floor_faces_raises_on_accessor_failure():
    class BadFace:
        def geomType(self):
            raise RuntimeError('injected floor accessor failure')

    with pytest.raises(targets.TargetError):
        targets.floor_faces([BadFace()], -10.0, 0.0)


def test_cylinder_faces_raises_on_accessor_failure():
    class BadFace:
        def geomType(self):
            raise RuntimeError('injected cylinder accessor failure')

    with pytest.raises(targets.TargetError):
        targets.cylinder_faces([BadFace()])


def test_accessor_exception_gives_error_status(monkeypatch, tmp_path):
    def boom(faces):
        raise RuntimeError('injected accessor failure')

    monkeypatch.setattr(targets, 'cylinder_faces', boom)
    spec = json.loads(json.dumps(TINY_CASE))
    spec['case_id'] = 'A01'
    result = targets.generate_case(spec, tmp_path, 1, preview=False)
    assert result['status'] == 'ERROR'
    assert result['reason_code'] == 'ERROR'
    assert not (tmp_path / 'A01' / 'case.json').exists()
    report = json.loads((tmp_path / 'A01' / 'validation.json').read_text(encoding='utf-8'))
    assert report['status'] == 'ERROR'


def test_imported_extra_root_never_passes(monkeypatch, tmp_path):
    original = targets.reimport_step

    def with_extra(path):
        roots = list(original(path))
        extra = targets.build_design({'stock': TINY_CASE['stock'], 'target': TINY_CASE['target']})['stock']
        return roots + [extra]

    monkeypatch.setattr(targets, 'reimport_step', with_extra)
    spec = json.loads(json.dumps(TINY_CASE))
    spec['case_id'] = 'R01'
    result = targets.generate_case(spec, tmp_path, 1, preview=False)
    assert result['status'] != 'PASS'
    assert not (tmp_path / 'R01' / 'case.json').exists()


def test_preview_failure_is_error(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise RuntimeError('injected preview failure')

    monkeypatch.setattr(targets, 'render_preview', boom)
    spec = json.loads(json.dumps(TINY_CASE))
    spec['case_id'] = 'V01'
    result = targets.generate_case(spec, tmp_path, 1, preview=True)
    assert result['status'] == 'ERROR'
    assert result['reason_code'] == 'ERROR'
    assert result['preview_error']
    assert not (tmp_path / 'V01' / 'case.json').exists()
    report = json.loads((tmp_path / 'V01' / 'validation.json').read_text(encoding='utf-8'))
    assert report['status'] == 'ERROR'
    assert report['preview']['error']


def test_overview_failure_is_error(monkeypatch, tmp_path):
    def boom(rows, path):
        raise RuntimeError('injected overview failure')

    monkeypatch.setattr(targets, 'render_overview', boom)
    root = tmp_path / 'out'
    result = targets.generate_batch(_batch_config(['O01']), root, master_seed=1, preview=True)
    assert result['status'] == 'ERROR'
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'ERROR'
    assert manifest['error']


def test_cli_preview_failure_nonzero(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise RuntimeError('injected preview failure')

    monkeypatch.setattr(targets, 'render_preview', boom)
    root = tmp_path / 'run'
    code = targets.main(['--config', str(CONFIG_PATH), '--case', 'S01', '--out', str(root)])
    assert code != 0
    assert (root / 'S01' / 'validation.json').is_file()
    assert not (root / 'S01' / 'case.json').exists()


def test_cli_overview_failure_nonzero(monkeypatch, tmp_path):
    def boom(rows, path):
        raise RuntimeError('injected overview failure')

    monkeypatch.setattr(targets, 'render_overview', boom)
    root = tmp_path / 'run'
    code = targets.main(['--config', str(CONFIG_PATH), '--case', 'S01', '--out', str(root)])
    assert code != 0
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'ERROR'
    assert manifest['error']


def test_s01_report_has_stock_roundtrip_and_numeric_error_fields(s01_run):
    report = json.loads((s01_run['root'] / 'S01' / 'validation.json').read_text(encoding='utf-8'))
    names = {check['name'] for check in report['checks']}
    for name in ('stock.reimport_bbox', 'stock.reimport_volume', 'target.reimport_bbox', 'target.reimport_volume'):
        assert name in names
    volume = next(check for check in report['checks'] if check['name'] == 'stock.reimport_volume')
    assert isinstance(volume['observed']['abs_error_mm3'], (int, float))
    bbox = next(check for check in report['checks'] if check['name'] == 'stock.reimport_bbox')
    assert isinstance(bbox['observed']['max_abs_error_mm'], (int, float))
    assert set(bbox['observed']['abs_error_mm']) == {'xmin', 'xmax', 'ymin', 'ymax', 'zmin', 'zmax'}
    analytic = next(check for check in report['checks'] if check['name'] == 'preexport.target_volume')
    assert isinstance(analytic['observed']['abs_error_mm3'], (int, float))


def test_floor_mean_cannot_certify_two_depths(monkeypatch):
    built = targets.build_design({'stock': TINY_CASE['stock'], 'target': TINY_CASE['target']})
    fake = {
        'count': 2,
        'z': -4.0,
        'z_min': -3.0,
        'z_max': -5.0,
        'xmin': 7.0, 'xmax': 17.0,
        'ymin': 5.0, 'ymax': 13.0,
    }
    monkeypatch.setattr(targets, 'floor_faces', lambda *args, **kwargs: fake)
    checks = targets.target_checks('t', built['design'], [built['target']], dict(targets.FROZEN_TOLERANCES))
    floor = next(check for check in checks if check['name'] == 't.target_floor_plane')
    assert not floor['pass']


def test_multiple_coplanar_floor_faces_are_valid(monkeypatch):
    built = targets.build_design({'stock': TINY_CASE['stock'], 'target': TINY_CASE['target']})
    fake = {
        'count': 2,
        'z': -4.0,
        'z_min': -4.0,
        'z_max': -4.0,
        'xmin': 7.0, 'xmax': 17.0,
        'ymin': 5.0, 'ymax': 13.0,
    }
    monkeypatch.setattr(targets, 'floor_faces', lambda *args, **kwargs: fake)
    checks = targets.target_checks('t', built['design'], [built['target']], dict(targets.FROZEN_TOLERANCES))
    floor = next(check for check in checks if check['name'] == 't.target_floor_plane')
    assert floor['pass']

# --- final corrections: case-id, API freeze, generic config, volume gate -----


def test_case_id_trailing_newline_rejected_before_io(tmp_path):
    root = tmp_path / 'out'
    spec = json.loads(json.dumps(TINY_CASE))
    spec['case_id'] = 'S01\n'
    with pytest.raises(targets.TargetError):
        targets.generate_case(spec, root, 1, preview=False)
    assert not root.exists()
    assert not (tmp_path / 'S01').exists()


@pytest.mark.parametrize('mutate', [
    lambda data: data['cases'][0]['target'].update(a=31),
    lambda data: data.update(master_seed=1),
    lambda data: data.update(units='cm'),
])
def test_generate_batch_api_enforces_frozen_config(tmp_path, mutate):
    data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    mutate(data)
    root = tmp_path / 'out'
    with pytest.raises(targets.TargetError):
        targets.generate_batch(data, root, preview=False)
    assert not root.exists()


def test_generate_batch_rejects_frozen_master_seed_override(tmp_path):
    data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    root = tmp_path / 'out'
    with pytest.raises(targets.TargetError):
        targets.generate_batch(data, root, master_seed=1, preview=False)
    assert not root.exists()


@pytest.mark.parametrize('mutate', [
    lambda data: data.update(units='cm'),
    lambda data: data['coordinates'].update(handedness='left'),
    lambda data: data['coordinates'].update(tool_axis=[0, 0, -1]),
    lambda data: data['coordinates'].update(top_z=False),
    lambda data: data['cases'][0].update(family_id='F2'),
    lambda data: data.update(seed_rule='uint64_le(SHA256(seed))'),
])
def test_generate_batch_generic_enforces_supported_frame(tmp_path, mutate):
    data = _batch_config(['G01'])
    mutate(data)
    root = tmp_path / 'out'
    with pytest.raises(targets.TargetError):
        targets.generate_batch(data, root, preview=False)
    assert not root.exists()


def test_generate_batch_generic_rejects_duplicate_case_ids(tmp_path):
    data = _batch_config(['D01', 'D01'])
    root = tmp_path / 'out'
    with pytest.raises(targets.TargetError):
        targets.generate_batch(data, root, preview=False)
    assert not root.exists()


def test_generate_batch_master_override_snapshot_and_input_unchanged(tmp_path):
    data = _batch_config(['M01'])
    before = json.loads(json.dumps(data))
    root = tmp_path / 'out'
    result = targets.generate_batch(data, root, master_seed=42, preview=False)
    assert result['status'] == 'PASS', result['error']
    assert data == before
    snapshot = json.loads((root / 'config_snapshot.json').read_text(encoding='utf-8'))
    assert snapshot['master_seed'] == 42
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['master_seed'] == 42
    case = json.loads((root / 'M01' / 'case.json').read_text(encoding='utf-8'))
    assert case['seed'] == targets.derive_seed(42, 'F1', 'M01')


def test_zero_reported_volume_never_certifies_solid(monkeypatch):
    built = targets.build_design({'stock': TINY_CASE['stock'], 'target': TINY_CASE['target']})
    measured = {
        'solid_count': 1,
        'valid': True,
        'finite': True,
        'volume': 0.0,
        'bbox': None,
        'faces': [],
    }
    monkeypatch.setattr(targets, 'measure_roots', lambda roots: dict(measured))
    monkeypatch.setattr(targets, 'analytic_pocket_volume', lambda *args: 0.0005)

    pocket = targets.pocket_checks('t', built['design'], [built['target']], dict(targets.FROZEN_TOLERANCES))
    assert not next(check for check in pocket if check['name'] == 't.pocket_solid')['pass']
    assert next(check for check in pocket if check['name'] == 't.pocket_volume')['pass']

    for checker_name in ('stock_checks', 'target_checks'):
        checks = getattr(targets, checker_name)('t', built['design'], [built['target']], dict(targets.FROZEN_TOLERANCES))
        solid = next(check for check in checks if check['name'].endswith('_solid'))
        assert not solid['pass']
