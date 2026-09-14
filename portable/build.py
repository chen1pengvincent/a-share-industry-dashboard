"""Build local, self-contained distributions. No networking or project mutation.

Use prepare_runtime.py first. This builder consumes only hash-bound runtime
assets and the exact, previously verified seed; every destination must be new.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
from pathlib import Path, PurePosixPath
import sys
import zipfile

SEED_ID = 'SWIVD2-RUN-20260904-001'
SEED_SHA = 'cfb0e20fa13fa76ea8fbdc1ac611b246f0f48bebd87e1b80f92bb03f522dba6a'
LEGACY_ID = 'SWIVD-RUN-20260828-004'
LEGACY_SHA = '5180fd5eb20e2cd556a5a5066039fba676272d735ae2d91b9b798e59b33daea0'
TARGETS = ('macos-arm64', 'macos-x86_64', 'windows-x86_64')
VERSION = '2.3.0-portable.1'
HERE = Path(__file__).resolve().parent


def linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def sha(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')


def safe_path(root: Path, value: str) -> Path:
    pure = PurePosixPath(value)
    if not value or not pure.parts or pure.is_absolute() or pure.as_posix() != value or any(
        part in ('', '.', '..') or re.search(r'[<>:"\\|?*\x00-\x1f]', part)
        or part.endswith((' ', '.')) or re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', part.split('.')[0], re.I)
        for part in pure.parts
    ):
        raise ValueError('PACKAGE_PATH_INVALID')
    target = root.joinpath(*pure.parts)
    for candidate in (target, *target.parents):
        if candidate == root:
            break
        if linked(candidate):
            raise ValueError('PACKAGE_SYMLINK_FORBIDDEN')
    return target


def verify_record(root: Path, record: dict):
    path = safe_path(root, record['path'])
    if not path.is_file() or path.stat().st_size != record['bytes'] or sha(path) != record['sha256']:
        raise ValueError('BUILD_INPUT_HASH_MISMATCH')


def inventory(root: Path):
    if linked(root) or not root.is_dir():
        raise ValueError('BUILD_INPUT_ROOT_INVALID')
    result = []
    for path in sorted(root.rglob('*')):
        safe_path(root, path.relative_to(root).as_posix())
        if linked(path):
            raise ValueError('PACKAGE_SYMLINK_FORBIDDEN')
        if path.is_file():
            result.append({'path': path.relative_to(root).as_posix(),
                           'bytes': path.stat().st_size, 'sha256': sha(path)})
    return result


def copy_tree(source: Path, destination: Path):
    # Do not follow links even if their target happens to be available here.
    if linked(source) or not source.is_dir():
        raise ValueError('BUILD_INPUT_SYMLINK')
    for path in source.rglob('*'):
        if linked(path):
            raise ValueError('BUILD_INPUT_SYMLINK')
    shutil.copytree(source, destination)


def verify_snapshot_inventory(root: Path, manifest: dict):
    records = manifest['artifacts']
    declared = {r['path'] for r in records}
    if len(declared) != len(records):
        raise ValueError('SNAPSHOT_DUPLICATE_ARTIFACT')
    observed = {r['path'] for r in inventory(root)}
    if observed != declared | {'manifest.json', 'SHA256SUMS'}:
        raise ValueError('SNAPSHOT_INVENTORY_MISMATCH')
    for record in records:
        verify_record(root, record)


def source_and_seed(project: Path, seed_data: Path, output: Path):
    seed_run = seed_data / 'runs' / SEED_ID
    if sha(seed_run / 'manifest.json') != SEED_SHA:
        raise ValueError('SEED_IDENTITY_MISMATCH')
    manifest = read(seed_run / 'manifest.json')
    if manifest['purpose'] != 'UPDATE_LATEST' or manifest['decision_eligible'] or manifest['production_approved']:
        raise ValueError('SEED_SCOPE_INVALID')
    verify_snapshot_inventory(seed_run, manifest)
    app = output / 'app'
    app.mkdir()
    source_paths = []
    for record in manifest['source_files']:
        verify_record(project, record)
        source_paths.append(record['path'])
    # Retain the v1 CLI's original spec, without changing its historical paths.
    source_paths.append('PROJECT_SPEC.json')
    for relative in sorted(set(source_paths)):
        source = safe_path(project, relative)
        destination = safe_path(app, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    legacy = project / 'output' / 'runs' / LEGACY_ID
    if sha(legacy / 'manifest.json') != LEGACY_SHA:
        raise ValueError('LEGACY_IDENTITY_MISMATCH')
    verify_snapshot_inventory(legacy, read(legacy / 'manifest.json'))
    (app / 'output' / 'runs').mkdir(parents=True)
    copy_tree(legacy, app / 'output' / 'runs' / LEGACY_ID)
    seed_out = output / 'seed-data'
    (seed_out / 'runs').mkdir(parents=True)
    copy_tree(seed_run, seed_out / 'runs' / SEED_ID)
    pointer = read(seed_data / 'latest_run.json')
    if pointer['run_id'] != SEED_ID or pointer['target_sha256'] != SEED_SHA:
        raise ValueError('SEED_POINTER_MISMATCH')
    shutil.copy2(seed_data / 'latest_run.json', seed_out / 'latest_run.json')
    lines = (seed_data / 'run_ledger.ndjson').read_bytes().splitlines(keepends=True)
    selected = [line for line in lines if json.loads(line).get('run_id') == SEED_ID]
    if len(selected) != 1 or json.loads(selected[0]).get('event') != 'RUN_SUCCEEDED':
        raise ValueError('SEED_LEDGER_MISMATCH')
    (seed_out / 'run_ledger.ndjson').write_bytes(selected[0])


def build(*, project: Path, seed_data: Path, assets: Path, output_dir: Path, target: str) -> dict:
    package_name = f'SWIVD-{VERSION}-{target}'
    output = output_dir / package_name
    archive = output_dir / (package_name + '.zip')
    if output.exists() or archive.exists():
        raise ValueError('DESTINATION_ALREADY_EXISTS')
    asset_root = assets / target
    asset = read(asset_root / 'asset-manifest.json')
    if asset['target'] != target or asset['python_version'] != '3.14.6':
        raise ValueError('RUNTIME_IDENTITY_MISMATCH')
    records = asset['files']
    expected_paths = {record['path'] for record in records}
    if len(expected_paths) != len(records):
        raise ValueError('DUPLICATE_ASSET_PATH')
    actual = {f'{name}/{record["path"]}' for name in ('runtime', 'packages')
              for record in inventory(asset_root / name)}
    if expected_paths != actual:
        raise ValueError('ASSET_INVENTORY_MISMATCH')
    for record in records:
        verify_record(asset_root, record)
    if asset.get('requirements_sha256') != sha(project / 'requirements.lock'):
        raise ValueError('ASSET_LOCK_MISMATCH')
    sources = read(HERE / 'runtime-sources.json')
    if asset.get('runtime_source') != sources['targets'][target]:
        raise ValueError('ASSET_UPSTREAM_MISMATCH')
    if target.startswith('macos-'):
        if asset.get('macho_dependencies', {}).get('status') != 'PASS':
            raise ValueError('ASSET_NATIVE_DEPENDENCIES_UNVERIFIED')
    else:
        review = asset.get('windows_dependencies', {})
        if not review.get('binary_count') or review.get('unresolved_non_system_names') != []:
            raise ValueError('ASSET_NATIVE_DEPENDENCIES_UNVERIFIED')
    output.mkdir(parents=True)
    source_and_seed(project, seed_data, output)
    for name in ('runtime', 'packages'):
        copy_tree(asset_root / name, output / name)
    for name in ('launcher.py', 'verify_delivery.py', 'README-PORTABLE.md', 'THIRD-PARTY.md'):
        shutil.copy2(HERE / name, output / name)
    script = 'Start-Windows.cmd' if target.startswith('windows') else 'Start-Mac.command'
    shutil.copy2(HERE / script, output / script)
    if not target.startswith('windows'):
        (output / script).chmod(0o755)
    shutil.copy2(asset_root / 'asset-manifest.json', output / 'runtime-manifest.json')
    shutil.copy2(HERE / 'runtime-sources.json', output / 'runtime-sources.json')
    executable = 'runtime/python.exe' if target.startswith('windows') else 'runtime/bin/python3.14'
    package = {'schema_version': 'swivd-portable-v1', 'version': VERSION, 'target': target,
               'python_version': '3.14.6', 'python_executable': executable,
               'seed_run_id': SEED_ID, 'seed_manifest_sha256': SEED_SHA, 'seed_as_of': '20260904',
               'decision_id': 'GOV-20260906-004', 'research_grade': 'RESEARCH_ONLY',
               'decision_eligible': False, 'production_approved': False,
               'windows_e2e': 'WINDOWS_E2E_UNVERIFIED',
               'minimum_os': {'macos-arm64': 'macOS 11', 'macos-x86_64': 'macOS 10.15',
                              'windows-x86_64': 'Windows 10 x64'}[target],
               'data_directory': 'data/state',
               'files': inventory(output)}
    write(output / 'package-manifest.json', package)
    # ZIP preserves Unix execute bits for macOS. No symlinks, caches, or user data.
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for record in inventory(output):
            path = output / record['path']
            info = zipfile.ZipInfo(package_name + '/' + record['path'], (2026, 9, 6, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open('rb') as source, bundle.open(info, 'w', force_zip64=True) as sink:
                shutil.copyfileobj(source, sink)
    result = {'target': target, 'archive': archive.name, 'bytes': archive.stat().st_size,
              'sha256': sha(archive), 'manifest_sha256': sha(output / 'package-manifest.json'),
              'package_files': len(package['files']), 'seed_run_id': SEED_ID}
    (output_dir / (archive.name + '.sha256')).write_text(result['sha256'] + '  ' + archive.name + '\n', encoding='ascii')
    write(output_dir / (package_name + '-build.json'), result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--seed-data', type=Path, required=True)
    parser.add_argument('--assets-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--target', choices=TARGETS, action='append')
    args = parser.parse_args()
    for target in args.target or TARGETS:
        print(json.dumps(build(project=args.project.resolve(), seed_data=args.seed_data.resolve(),
              assets=args.assets_dir.resolve(), output_dir=args.output_dir.resolve(), target=target)), flush=True)


if __name__ == '__main__':
    main()
