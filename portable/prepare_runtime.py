#!/usr/bin/env python3
"""Build fixed, offline-ready runtime assets without installing into the host.

Build prerequisites: a Python 3.11+ interpreter with pip and certifi. Target
machines do not need those tools. Run with -I -B. This module never reads tokens.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import ssl
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile


HERE = Path(__file__).resolve().parent


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def lock_entries(path: Path) -> dict[str, dict]:
    result: dict[str, dict] = {}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", line)
        if match:
            current = match.group(1).lower().replace("_", "-")
            result[current] = {"version": match.group(2), "hashes": []}
        elif line.strip().startswith("--hash=") and current:
            result[current]["hashes"].append(line.strip().split("sha256:")[1].split()[0])
    if len(result) != 20 or any(not value["hashes"] for value in result.values()):
        raise ValueError("LOCK_SCHEMA_OR_PACKAGE_COUNT")
    return result


class RuntimeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # GitHub Release assets require a signed redirect onto its own CDN.
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in {
            "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
            "www.python.org", "pypi.org", "files.pythonhosted.org",
        } or parsed.username or parsed.password:
            raise ValueError("UNAPPROVED_RUNTIME_REDIRECT")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_runtime(source: dict, downloads: Path, ca_file: str) -> Path:
    destination = downloads / urllib.parse.unquote(source["url"].rsplit("/", 1)[1])
    if destination.exists():
        if digest(destination) != source["sha256"]:
            raise ValueError("CACHED_RUNTIME_HASH_MISMATCH")
        return destination
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)),
        RuntimeRedirect(),
    )
    part = destination.with_suffix(destination.suffix + ".part")
    start = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": "SWIVD-portable-builder/1"}
    if start:
        headers["Range"] = f"bytes={start}-"
    request = urllib.request.Request(source["url"], headers=headers)
    with opener.open(request, timeout=90) as response:
        append = start and response.status == 206
        if append and not response.headers.get("Content-Range", "").startswith(f"bytes {start}-"):
            raise ValueError("RUNTIME_RANGE_MISMATCH")
        with part.open("ab" if append else "wb") as output:
            shutil.copyfileobj(response, output)
    if digest(part) != source["sha256"]:
        raise ValueError("RUNTIME_HASH_MISMATCH")
    if source.get("size") is not None and part.stat().st_size != source["size"]:
        raise ValueError("RUNTIME_SIZE_MISMATCH")
    part.replace(destination)
    return destination


def safe_relative(name: str) -> PurePosixPath:
    relative = PurePosixPath(name)
    if not name or relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
        raise ValueError("UNSAFE_ARCHIVE_PATH")
    return relative


def extract_runtime(archive: Path, source: dict, destination: Path) -> dict:
    unpack = Path(tempfile.mkdtemp(prefix=".runtime-unpack-", dir=destination.parent))
    try:
        if archive.name.endswith(".zip"):
            with zipfile.ZipFile(archive) as package:
                for entry in package.infolist():
                    safe_relative(entry.filename)
                    if stat.S_ISLNK(entry.external_attr >> 16):
                        raise ValueError("ZIP_RUNTIME_SYMLINK")
                package.extractall(unpack)
        else:
            with tarfile.open(archive, "r:gz") as package:
                for entry in package.getmembers():
                    safe_relative(entry.name)
                package.extractall(unpack, filter="data")
        original = (unpack / source["archive_root"]).resolve()
        if not original.is_relative_to(unpack):
            raise ValueError("RUNTIME_ROOT_ESCAPE")
        for path in original.rglob("*"):
            if not path.resolve().is_relative_to(original):
                raise ValueError("RUNTIME_LINK_ESCAPE")
            if not path.is_dir() and not path.is_file():
                raise ValueError("RUNTIME_SPECIAL_FILE")
        shutil.copytree(original, destination, symlinks=False)
    finally:
        # This exact directory was created by this function, not supplied by users.
        shutil.rmtree(unpack)
    if not (destination / source["executable"]).is_file():
        raise ValueError("RUNTIME_EXECUTABLE_MISSING")
    modifications = {"internal_symlinks": "dereferenced to regular files"}
    if source["platform"] == "win_amd64":
        pth = destination / "python314._pth"
        original_pth_sha256 = digest(pth)
        pth.write_text("python314.zip\n.\n../packages\n", encoding="utf-8")
        modifications["python314._pth"] = {
            "original_sha256": original_pth_sha256,
            "reason": "explicit adjacent package path; site and arbitrary .pth execution disabled",
        }
        for name in ("vcruntime140.dll", "vcruntime140_1.dll"):
            if not (destination / name).is_file():
                raise ValueError("WINDOWS_VCRUNTIME_MISSING")
    return modifications


def prepare_wheels(lock: Path, wheelhouse: Path, source: dict, ca_file: str) -> None:
    # Use pip's target-tag compatibility engine but not its expensive live index
    # resolution: every dependency and allowable hash is already frozen in lock.
    from pip._internal.models.target_python import TargetPython
    from pip._vendor.packaging.utils import parse_wheel_filename
    tags = TargetPython(platforms=[source["platform"]], py_version_info=(3, 14, 6),
                        abis=["cp314"], implementation="cp").get_sorted_tags()
    priorities = {tag: index for index, tag in enumerate(tags)}
    locked = lock_entries(lock)
    found = set()
    for wheel in wheelhouse.glob("*.whl"):
        name, version, _, wheel_tags = parse_wheel_filename(wheel.name)
        if name not in locked or str(version) != locked[name]["version"] or digest(wheel) not in locked[name]["hashes"]:
            raise ValueError("CACHED_WHEEL_HASH_OR_VERSION_MISMATCH")
        if not set(priorities).intersection(wheel_tags):
            raise ValueError("CACHED_WHEEL_TARGET_INCOMPATIBLE")
        found.add(name)
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)),
        RuntimeRedirect(),
    )
    sources_path = wheelhouse / "wheel-download-sources.json"
    records = json.loads(sources_path.read_text(encoding="utf-8")) if sources_path.exists() else {}
    for name, entry in locked.items():
        if name in found:
            continue
        metadata_url = f"https://pypi.org/pypi/{name}/{entry['version']}/json"
        request = urllib.request.Request(metadata_url, headers={"User-Agent": "SWIVD-portable-builder/1"})
        with opener.open(request, timeout=45) as response:
            metadata = json.load(response)
        candidates = []
        for asset in metadata["urls"]:
            if asset["packagetype"] != "bdist_wheel" or asset["yanked"]:
                continue
            package, version, _, wheel_tags = parse_wheel_filename(asset["filename"])
            matches = set(priorities).intersection(wheel_tags)
            if package == name and str(version) == entry["version"] and matches and asset["digests"]["sha256"] in entry["hashes"]:
                candidates.append((min(priorities[tag] for tag in matches), asset["filename"], asset))
        if not candidates:
            raise ValueError("NO_LOCKED_COMPATIBLE_WHEEL")
        asset = min(candidates, key=lambda item: item[:2])[2]
        parsed = urllib.parse.urlsplit(asset["url"])
        if parsed.scheme != "https" or parsed.hostname != "files.pythonhosted.org" or parsed.username or parsed.password:
            raise ValueError("UNAPPROVED_WHEEL_HOST")
        print(source["platform"] + " FETCH_WHEEL " + name, flush=True)
        destination = wheelhouse / asset["filename"]
        part = destination.with_suffix(".whl.part")
        start = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": "SWIVD-portable-builder/1"}
        if start:
            headers["Range"] = f"bytes={start}-"
        request = urllib.request.Request(asset["url"], headers=headers)
        with opener.open(request, timeout=90) as response:
            append = start and response.status == 206
            if append and not response.headers.get("Content-Range", "").startswith(f"bytes {start}-"):
                raise ValueError("WHEEL_RANGE_MISMATCH")
            with part.open("ab" if append else "wb") as output:
                shutil.copyfileobj(response, output)
        if digest(part) != asset["digests"]["sha256"]:
            raise ValueError("WHEEL_HASH_MISMATCH")
        part.replace(destination)
        records[asset["filename"]] = {"url": asset["url"], "metadata_url": metadata_url,
                                     "sha256": asset["digests"]["sha256"]}
        sources_path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")


def extract_wheels(wheelhouse: Path, packages: Path, locked: dict) -> list[dict]:
    manifests = []
    found = set()
    packages.mkdir()
    sources_path = wheelhouse / "wheel-download-sources.json"
    wheel_sources = json.loads(sources_path.read_text(encoding="utf-8")) if sources_path.exists() else {}
    for wheel in sorted(wheelhouse.glob("*.whl")):
        name, version = wheel.name.split("-", 2)[:2]
        name = name.lower().replace("_", "-")
        wheel_sha = digest(wheel)
        if name not in locked or version != locked[name]["version"] or wheel_sha not in locked[name]["hashes"]:
            raise ValueError("WHEEL_NOT_IN_LOCK")
        if name in found:
            raise ValueError("DUPLICATE_PACKAGE_WHEEL")
        found.add(name)
        records = []
        omitted = []
        with zipfile.ZipFile(wheel) as package:
            for entry in package.infolist():
                path = safe_relative(entry.filename)
                if entry.is_dir():
                    continue
                if stat.S_ISLNK(entry.external_attr >> 16):
                    raise ValueError("WHEEL_SYMLINK")
                if path.parts[0].endswith(".data"):
                    if len(path.parts) < 3:
                        raise ValueError("WHEEL_DATA_LAYOUT")
                    if path.parts[1] in {"purelib", "platlib"}:
                        path = PurePosixPath(*path.parts[2:])
                    elif path.parts[1] == "scripts":
                        omitted.append(str(path))
                        continue
                    else:
                        raise ValueError("UNSUPPORTED_WHEEL_DATA_SCHEME")
                destination = packages.joinpath(*path.parts)
                if destination.exists():
                    raise ValueError("WHEEL_FILE_COLLISION")
                destination.parent.mkdir(parents=True, exist_ok=True)
                payload = package.read(entry)
                destination.write_bytes(payload)
                mode = entry.external_attr >> 16
                destination.chmod(0o755 if mode & 0o111 else 0o644)
                records.append(str(path))
        manifests.append({"name": name, "version": version, "wheel": wheel.name, "sha256": wheel_sha,
                          "source": "https://pypi.org/pypi/" + name + "/" + version + "/json",
                          "download_url": wheel_sources.get(wheel.name, {}).get("url"),
                          "retrieval": "fixed_upstream_url" if wheel.name in wheel_sources else "previously_downloaded_original_wheel_verified_against_lock",
                          "installed_files": len(records), "omitted_console_scripts": omitted})
    if found != set(locked):
        raise ValueError("LOCKED_PACKAGE_SET_INCOMPLETE")
    return manifests


def inventory(root: Path) -> list[dict]:
    return [{"path": path.relative_to(root).as_posix(), "sha256": digest(path), "bytes": path.stat().st_size}
            for path in sorted(root.rglob("*")) if path.is_file()]


def asset_inventory(root: Path) -> list[dict]:
    return [{**entry, "path": section + "/" + entry["path"]}
            for section in ("runtime", "packages") for entry in inventory(root / section)]


def pe_imports(path: Path) -> list[str]:
    """Read normal and delay import DLL names without executing Windows code."""
    data = path.read_bytes()
    if data[:2] != b"MZ":
        return []
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("PE_SIGNATURE")
    machine, sections = struct.unpack_from("<HH", data, pe + 4)
    if machine != 0x8664:
        raise ValueError("PE_ARCHITECTURE_MISMATCH")
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    if struct.unpack_from("<H", data, optional)[0] != 0x20B:
        raise ValueError("PE64_REQUIRED")
    section_table = optional + optional_size
    mapping = []
    for index in range(sections):
        virtual_size, virtual_address, raw_size, raw_address = struct.unpack_from(
            "<IIII", data, section_table + index * 40 + 8)
        mapping.append((virtual_address, max(virtual_size, raw_size), raw_address))
    def offset(rva):
        for start, size, raw in mapping:
            if start <= rva < start + size:
                return raw + rva - start
        raise ValueError("PE_RVA_RANGE")
    def name(rva):
        start = offset(rva)
        return data[start:data.index(b"\0", start)].decode("ascii").lower()
    names = set()
    for directory_index, descriptor_size, name_offset in ((1, 20, 12), (13, 32, 4)):
        rva, size = struct.unpack_from("<II", data, optional + 112 + directory_index * 8)
        if not rva or not size:
            continue
        table = offset(rva)
        for index in range(size // descriptor_size + 1):
            position = table + index * descriptor_size
            descriptor = data[position:position + descriptor_size]
            if descriptor == b"\0" * descriptor_size:
                break
            if directory_index == 13 and struct.unpack_from("<I", descriptor)[0] & 1 == 0:
                raise ValueError("UNSUPPORTED_PE_DELAY_VA")
            names.add(name(struct.unpack_from("<I", descriptor, name_offset)[0]))
    return sorted(names)


def windows_dependencies(root: Path) -> dict:
    binaries = [path for path in root.rglob("*") if path.suffix.lower() in {".dll", ".pyd", ".exe"}]
    provided = {path.name.lower() for path in binaries}
    system = {"kernel32.dll", "ntdll.dll", "advapi32.dll", "ws2_32.dll", "user32.dll", "ole32.dll",
              "oleaut32.dll", "shell32.dll", "shlwapi.dll", "bcrypt.dll", "crypt32.dll", "version.dll",
              "msvcrt.dll", "ucrtbase.dll", "secur32.dll", "rpcrt4.dll", "gdi32.dll", "comdlg32.dll",
              "comctl32.dll", "winmm.dll", "psapi.dll", "userenv.dll", "iphlpapi.dll", "dbghelp.dll",
              "pathcch.dll", "normaliz.dll", "powrprof.dll", "propsys.dll"}
    imported = {path.relative_to(root).as_posix(): pe_imports(path) for path in binaries}
    unresolved = sorted({name for names in imported.values() for name in names
                         if name not in provided | system and not name.startswith(("api-ms-win-", "ext-ms-win-"))})
    return {"method": "PE64 import and delay-import tables; static only, DLL search behavior not executed",
            "binary_count": len(binaries), "provided_dll_names": sorted(provided),
            "unresolved_non_system_names": unresolved, "imports": imported,
            "live_validation_state": "WINDOWS_E2E_UNVERIFIED"}


def macho_dependencies(root: Path, source: dict) -> dict:
    """Inspect actual target Mach-O load commands, not only wheel filename tags."""
    tool = shutil.which("otool")
    if tool is None:
        return {"status": "MACHO_STATIC_UNVERIFIED", "reason": "OTOOL_UNAVAILABLE"}
    architecture = source["architecture"]
    minimums = []
    report = []
    issues = []
    magic_numbers = {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
                     b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
    for section in ("runtime", "packages"):
        for path in sorted((root / section).rglob("*")):
            if not path.is_file():
                continue
            with path.open("rb") as stream:
                if stream.read(4) not in magic_numbers:
                    continue
            command = [tool, "-arch", architecture, "-L", str(path)]
            links = subprocess.run(command, capture_output=True, text=True, check=True).stdout
            loads = subprocess.run([tool, "-arch", architecture, "-l", str(path)],
                                   capture_output=True, text=True, check=True).stdout
            dependencies = []
            rpaths = []
            image_id = None
            # -L includes LC_ID_DYLIB (the image's own install name). Parse load
            # command kinds from -l so an @rpath self-ID is not a false missing DLL.
            for block in re.split(r"\nLoad command \d+\n", loads):
                kind = re.search(r"\bcmd (LC_[A-Z_]+)", block)
                if not kind:
                    continue
                value = re.search(r"\b(?:name|path) (.+?) \(offset \d+\)", block)
                if not value:
                    continue
                if kind.group(1) in {"LC_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB", "LC_REEXPORT_DYLIB",
                                      "LC_LAZY_LOAD_DYLIB", "LC_LOAD_UPWARD_DYLIB"}:
                    dependencies.append(value.group(1))
                elif kind.group(1) == "LC_RPATH":
                    rpaths.append(value.group(1))
                elif kind.group(1) == "LC_ID_DYLIB":
                    image_id = value.group(1)
            versions = re.findall(r"cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version (\d+(?:\.\d+)+)", loads)
            versions += re.findall(r"cmd LC_BUILD_VERSION\s+cmdsize \d+\s+platform (?:1|MACOS)\s+minos (\d+(?:\.\d+)+)", loads)
            minimums.extend(versions)
            if not versions:
                issues.append({"path": path.relative_to(root).as_posix(),
                               "reason": "TARGET_ARCH_OR_MINIMUM_VERSION_MISSING"})
            def expanded(value):
                if value.startswith("@loader_path/"):
                    return (path.parent / value.removeprefix("@loader_path/")).resolve()
                if value.startswith("@executable_path/"):
                    return (root / "runtime" / "bin" / value.removeprefix("@executable_path/")).resolve()
                if value.startswith("/"):
                    return Path(value).resolve()
                return None
            for dependency in dependencies:
                if dependency.startswith(("/usr/lib/", "/System/Library/")):
                    continue
                if dependency.startswith(("@loader_path/", "@executable_path/")):
                    resolved = expanded(dependency)
                    if resolved is None or not resolved.is_relative_to(root) or not resolved.exists():
                        issues.append({"path": path.relative_to(root).as_posix(), "dependency": dependency,
                                       "reason": "LOADER_PATH_UNRESOLVED_OR_ESCAPE"})
                elif dependency.startswith("@rpath/"):
                    candidates = [base / dependency.removeprefix("@rpath/")
                                  for value in rpaths if (base := expanded(value)) is not None]
                    if not any(candidate.resolve().is_relative_to(root) and candidate.is_file() for candidate in candidates):
                        issues.append({"path": path.relative_to(root).as_posix(), "dependency": dependency,
                                       "reason": "RPATH_REQUIRES_REVIEW", "rpaths": rpaths})
                else:
                    issues.append({"path": path.relative_to(root).as_posix(), "dependency": dependency,
                                   "reason": "NON_SYSTEM_ABSOLUTE_OR_UNSUPPORTED_PATH"})
            report.append({"path": path.relative_to(root).as_posix(), "minimum_versions": versions,
                           "dependencies": dependencies, "rpaths": rpaths, "image_id": image_id})
    def version_tuple(value):
        parts = tuple(map(int, value.split(".")))
        return parts + (0,) * (3 - len(parts))
    actual_minimum = max(minimums, key=version_tuple) if minimums else None
    expected = source["minimum_os"].split()[-1]
    if not report or not actual_minimum or version_tuple(actual_minimum) > version_tuple(expected):
        issues.append({"reason": "ACTUAL_MINIMUM_MISSING_OR_EXCEEDS_TARGET", "actual_minimum": actual_minimum})
    return {"status": "PASS" if not issues else "MACHO_REVIEW_REQUIRED", "architecture": architecture,
            "method": "otool -arch TARGET -L and -l on every actual Mach-O file",
            "binary_count": len(report), "actual_minimum_os": actual_minimum,
            "issues": issues, "binaries": report}


def build_target(target: str, source: dict, assets: Path, lock: Path, ca_file: str) -> None:
    target_root = assets / target
    manifest_path = target_root / "asset-manifest.json"
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if old["requirements_sha256"] != digest(lock) or old["runtime_source"] != source:
            raise ValueError("EXISTING_ASSET_SOURCE_MISMATCH")
        if asset_inventory(target_root) != old["files"]:
            raise ValueError("EXISTING_ASSET_HASH_MISMATCH")
        if target.startswith("macos-") and "macho_dependencies" not in old:
            old["macho_dependencies"] = macho_dependencies(target_root, source)
            manifest_path.write_text(json.dumps(old, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if target.startswith("macos-") and old["macho_dependencies"]["status"] != "PASS":
            raise ValueError("MACHO_DEPENDENCY_REVIEW_REQUIRED")
        print(target + " ASSETS_VERIFIED", flush=True)
        return
    target_root.mkdir(parents=True, exist_ok=True)
    if (target_root / "runtime").exists() or (target_root / "packages").exists():
        raise ValueError("INCOMPLETE_ASSET_TARGET_REQUIRES_NEW_DIRECTORY")
    print(target + " DOWNLOADING_RUNTIME", flush=True)
    archive = fetch_runtime(source, assets / "downloads", ca_file)
    wheelhouse = target_root / "wheelhouse"
    wheelhouse.mkdir(exist_ok=True)
    print(target + " DOWNLOADING_LOCKED_WHEELS", flush=True)
    prepare_wheels(lock, wheelhouse, source, ca_file)
    modifications = extract_runtime(archive, source, target_root / "runtime")
    wheels = extract_wheels(wheelhouse, target_root / "packages", lock_entries(lock))
    manifest = {"schema_version": 1, "target": target, "python_version": "3.14.6",
                "requirements_sha256": digest(lock), "runtime_source": source,
                "runtime_modifications": modifications, "wheels": wheels,
                "platform_minimum": source["minimum_os"], "files": asset_inventory(target_root)}
    if target == "windows-x86_64":
        manifest["windows_dependencies"] = windows_dependencies(target_root)
        if manifest["windows_dependencies"]["unresolved_non_system_names"]:
            (target_root / "windows-dependency-review.json").write_text(
                json.dumps(manifest["windows_dependencies"], indent=2), encoding="utf-8")
            raise ValueError("WINDOWS_DEPENDENCY_REVIEW_REQUIRED")
    else:
        manifest["macho_dependencies"] = macho_dependencies(target_root, source)
        if manifest["macho_dependencies"]["status"] != "PASS":
            (target_root / "macho-dependency-review.json").write_text(
                json.dumps(manifest["macho_dependencies"], indent=2), encoding="utf-8")
            raise ValueError("MACHO_DEPENDENCY_REVIEW_REQUIRED")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(target + " ASSETS_PREPARED", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-dir", type=Path, required=True)
    parser.add_argument("--target", action="append", choices=("macos-arm64", "macos-x86_64", "windows-x86_64"))
    args = parser.parse_args()
    sources = json.loads((HERE / "runtime-sources.json").read_text(encoding="utf-8"))
    assets = args.assets_dir.resolve()
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "downloads").mkdir(exist_ok=True)
    import certifi
    for target in args.target or sources["targets"]:
        build_target(target, sources["targets"][target], assets, HERE.parent / "requirements.lock", certifi.where())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, tarfile.TarError, subprocess.SubprocessError) as error:
        # This script does not handle credentials. Still avoid arbitrary upstream bodies.
        print("ASSET_PREPARATION_FAILED " + type(error).__name__ + ": " + (
            str(error) if re.fullmatch(r"[A-Z][A-Z0-9_]+", str(error)) else "SEE_SAFE_BUILD_STAGE"
        ), file=sys.stderr)
        raise SystemExit(1) from None
