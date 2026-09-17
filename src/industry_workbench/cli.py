"""Explicit local CLI. Diagnostic commands do not initialize or fetch data."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
import tempfile

SOURCE_ROOT = Path(__file__).resolve().parents[2]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="A 股行业估值与资金流本地研究工作台")
    commands = result.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("serve", "启动本机统一网页；应用存活期间自动更新"),
        ("update", "联网更新最近可用交易日；三页共用一个批次"),
        ("backfill", "按真实历史归属持续回补指定日期范围"),
        ("doctor", "只读诊断依赖、凭据是否配置及源码状态；不验证联网权限"),
        ("verify", "只读验证批次 manifest、对象及冻结源码哈希闭包"),
        ("import-legacy", "验证并复制旧完整快照；不修改来源，不覆盖已有导入"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--data-dir", help="独立数据根；默认 macOS Application Support/ashare-industry")
        command.add_argument("--development", action="store_true", help="仅允许临时目录或 .local/evidence；结果不属于正式快照")
        if name == "serve":
            command.add_argument("--port", type=int, default=8765)
            command.add_argument("--no-scheduler", action="store_true", help="关闭自动更新及历史回补，允许离线浏览")
        elif name == "backfill":
            command.add_argument("--start-date", required=True, help="YYYYMMDD")
            command.add_argument("--end-date", required=True, help="YYYYMMDD")
            command.add_argument("--max-days", type=int, help="显式限制本次回补交易日数（1–40）；省略时持续处理范围")
            command.add_argument("--retry-failed", action="store_true", help="显式重试所选范围内已失败的日期；每个日期本次最多重试一次")
        elif name == "verify":
            command.add_argument("--batch-id", help="省略时验证 current 明确指向的批次")
        elif name == "import-legacy":
            command.add_argument("--source", required=True, help="旧数据根，或完整 UPDATE_LATEST 快照目录")
    return result


def data_root(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ashare-industry"
    return Path.home() / ".local" / "share" / "ashare-industry"


def dependency_status(source_root: Path = SOURCE_ROOT) -> dict:
    from .runtime import dependency_status as runtime_dependency_status
    return runtime_dependency_status(source_root)


def doctor(root: Path, *, development=False, source_root: Path = SOURCE_ROOT) -> dict:
    from swivd.runtime_environment import timezone_available
    from swivd.tushare_client import load_tushare_token
    from .storage import FileStore, source_identity
    configured = False
    # Suppress diagnostics from Tushare's inherited local getter. Return only
    # a boolean; neither a credential nor a raw exception is exposed.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            configured = bool(load_tushare_token())
        except Exception:
            pass
    source = source_identity(source_root)
    store = FileStore(root, development=development)
    dependencies = dependency_status(source_root)
    legacy_root = (root / "latest_run.json").exists()
    return {"python_supported": sys.implementation.name == "cpython" and (3, 11) <= sys.version_info[:2] < (3, 15),
            "platform_is_macos": sys.platform == "darwin", "timezone_available": timezone_available(),
            **dependencies, "token_configured": configured, "token_validity": "NOT_CHECKED",
            "network_checked": False, "data_dir_exists": store.root.is_dir(),
            "legacy_data_root_detected": legacy_root,
            "development": development, "git_commit_present": bool(source["commit"]), "git_dirty": source["git_dirty"],
            "formal_source_eligible": bool(source["commit"]) and not source["git_dirty"],
            "formal_update_ready": bool(source["commit"]) and not source["git_dirty"] and not legacy_root
                                   and configured and dependencies["dependencies_locked"],
            "legacy_archive_required_for_new_data": False}


def verify_batch(store, batch_id: str | None = None) -> dict:
    from .models import DataError, json_bytes
    manifest = store.manifest(batch_id) if batch_id else store.current()
    if manifest is None:
        return {"status": "EMPTY", "batch_id": None, "verified_objects": 0}
    source = manifest.get("source", {})
    if manifest.get("source_snapshot", {}).get("kind") != "source_snapshot_tar_gz":
        raise DataError("SOURCE_SNAPSHOT_REQUIRED")
    inventories = {}
    def bind_source(snapshot, inventory, identity):
        files = inventory.get("files")
        if not isinstance(files, dict) or not files or hashlib.sha256(json_bytes(files)).hexdigest() != inventory.get("tree_sha256"):
            raise DataError("SOURCE_INVENTORY_HASH_MISMATCH")
        if any(inventory.get(k) != identity.get(k) for k in ("tree_sha256", "commit", "git_dirty")):
            raise DataError("SOURCE_INVENTORY_IDENTITY_MISMATCH")
        if not isinstance(snapshot, dict) or snapshot.get("kind") != "source_snapshot_tar_gz":
            raise DataError("SOURCE_SNAPSHOT_REQUIRED")
        sha = snapshot["sha256"]
        if sha in inventories and inventories[sha] != files:
            raise DataError("SOURCE_SNAPSHOT_CONFLICT")
        inventories[sha] = files
    bind_source(manifest["source_snapshot"], source, source)
    def bind_embedded(value):
        if isinstance(value, dict):
            if {"identity", "inventory", "snapshot"} <= value.keys():
                bind_source(value["snapshot"], store.read_json(value["inventory"]), value["identity"])
            for item in value.values():
                bind_embedded(item)
        elif isinstance(value, list):
            for item in value:
                bind_embedded(item)
    bind_embedded(manifest)
    checked = set()
    snapshots = {}
    pending = list(store.refs(manifest))
    while pending:
        ref = pending.pop()
        if ref["sha256"] in checked:
            store.read_bytes(ref)
            continue
        content = store.read_bytes(ref)
        checked.add(ref["sha256"])
        if ref.get("kind") == "source_snapshot_tar_gz":
            snapshots[ref["sha256"]] = content
        else:
            try:
                value = json.loads(content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError):
                raise DataError("OBJECT_INVALID_JSON") from None
            bind_embedded(value)
            pending.extend(store.refs(value))
    for sha, content in snapshots.items():
        if sha not in inventories:
            raise DataError("SOURCE_SNAPSHOT_UNBOUND")
        source_files = inventories[sha]
        seen = set()
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            for member in archive:
                if not member.isfile() or member.name not in source_files or member.name in seen:
                    raise DataError("SOURCE_SNAPSHOT_IDENTITY_MISMATCH")
                seen.add(member.name)
                stream = archive.extractfile(member)
                if stream is None or hashlib.sha256(stream.read()).hexdigest() != source_files[member.name]:
                    raise DataError("SOURCE_SNAPSHOT_HASH_MISMATCH")
        if seen != set(source_files):
            raise DataError("SOURCE_SNAPSHOT_INCOMPLETE")
    if store.manifest(manifest["batch_id"]) != manifest:
        raise DataError("MANIFEST_CHANGED_DURING_VERIFY")
    return {"status": "VERIFIED", "batch_id": manifest["batch_id"], "verified_objects": len(checked),
            "as_of": manifest["as_of"], "publication_state": manifest["publication_state"],
            "artifact_publish_state": manifest.get("artifact_publish_state"),
            "live_validation_state": manifest.get("live_validation_state"),
            "research_grade": manifest.get("research_grade"), "decision_eligible": manifest.get("decision_eligible", False),
            "production_approved": manifest.get("production_approved", False)}


def import_legacy(store, source: Path) -> dict:
    from .legacy import LegacySnapshotReader
    from .models import DataError
    from swivd.v2_storage import publish_current
    source = source.expanduser().absolute()
    if source.is_symlink() or store.root.is_relative_to(source.resolve()) or source.resolve().is_relative_to(store.root):
        raise DataError("LEGACY_SOURCE_DESTINATION_OVERLAP")
    reader = LegacySnapshotReader(source)
    current = reader.current_snapshot()
    if not current["run_id"]:
        raise DataError("LEGACY_SNAPSHOT_NOT_FOUND")
    run_id = current["run_id"]
    run_root = reader.snapshot(run_id)
    manifest_bytes = (run_root / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("purpose") != "UPDATE_LATEST":
        raise DataError("LEGACY_CURRENT_SNAPSHOT_REQUIRED")
    # The old validator checks this inventory independently and deliberately
    # excludes it (and manifest.json) from manifest.artifacts. Preserve the
    # original bytes; never regenerate a receipt for the copied snapshot.
    checksum_file = run_root / "SHA256SUMS"
    if checksum_file.is_symlink() or not checksum_file.is_file():
        raise DataError("LEGACY_CHECKSUM_FILE_REQUIRED")
    checksum_bytes = checksum_file.read_bytes()
    destination = store.path("legacy")
    with store.writer():
        if destination.exists() or destination.is_symlink():
            raise DataError("LEGACY_ALREADY_IMPORTED")
        staging = Path(tempfile.mkdtemp(prefix=".legacy-import-", dir=store.root))
        try:
            target_run = staging / "runs" / run_id
            target_run.mkdir(parents=True)
            (target_run / "manifest.json").write_bytes(manifest_bytes)
            (target_run / "SHA256SUMS").write_bytes(checksum_bytes)
            for relative in sorted(reader.validated[run_id]):
                if relative == "manifest.json":
                    continue
                content = reader.verified_bytes(run_id, relative)
                target = target_run / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            if (run_root / "manifest.json").read_bytes() != manifest_bytes:
                raise DataError("LEGACY_MANIFEST_DRIFT")
            if LegacySnapshotReader(target_run).current_snapshot() != current:
                raise DataError("LEGACY_COPY_IDENTITY_MISMATCH")
            publish_current(staging, run_id=run_id, as_of=current["as_of"],
                            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest())
            if LegacySnapshotReader(staging).current_snapshot() != current:
                raise DataError("LEGACY_COPY_IDENTITY_MISMATCH")
            for path in staging.rglob("*"):
                if path.is_file():
                    with path.open("rb") as stream:
                        os.fsync(stream.fileno())
            os.rename(staging, destination)
            directory = os.open(store.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return {"status": "IMPORTED_READ_ONLY", **current, "source_unchanged": True}


def serve(store, *, port=8765, no_scheduler=False, source_root: Path = SOURCE_ROOT):
    from .jobs import JobManager, Pipeline
    from .scheduler import Scheduler
    from .server import WorkbenchApp, WorkbenchServer
    from .models import DataError
    if not 1 <= port <= 65535:
        raise DataError("INVALID_PORT")
    # Reading an empty instance creates neither a directory nor a lock.
    if store.path("transactions/publication.json").exists():
        with store.writer():
            pass
    jobs = JobManager(Pipeline(store, source_root))
    scheduler = Scheduler(jobs, enabled=not no_scheduler)
    server = WorkbenchServer(WorkbenchApp(source_root, jobs, scheduler), port=port)
    try:
        print(json.dumps({"status": "SERVING", "url": server.origin, "scheduler_enabled": scheduler.enabled,
                          "development": store.development}, ensure_ascii=False), flush=True)
        scheduler.start()
        server.serve_forever(poll_interval=0.3)
    finally:
        scheduler.stop()
        jobs.stop()
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        root = data_root(args.data_dir)
        if args.command == "doctor":
            result = doctor(root, development=args.development)
            code = 0 if result["python_supported"] and result["dependencies_locked"] else 1
        else:
            from .models import DataError
            from .storage import FileStore
            if (root / "latest_run.json").exists():
                raise DataError("LEGACY_DATA_ROOT_REQUIRES_IMPORT")
            store = FileStore(root, development=args.development)
            if args.command == "serve":
                serve(store, port=args.port, no_scheduler=args.no_scheduler)
                return 0
            if args.command == "verify":
                result = verify_batch(store, args.batch_id)
            elif args.command == "import-legacy":
                result = import_legacy(store, Path(args.source))
            else:
                from .jobs import JobManager, Pipeline
                params = {} if args.command == "update" else {"start_date": args.start_date, "end_date": args.end_date,
                                                            "retry_failed": args.retry_failed}
                if args.command == "backfill" and args.max_days is not None:
                    params["max_days"] = args.max_days
                result = JobManager(Pipeline(store, SOURCE_ROOT)).submit(args.command, params, asynchronous=False)
            code = 1 if result.get("status") == "FAILED" else 0
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return code
    except KeyboardInterrupt:
        print("已停止。", file=sys.stderr)
        return 130
    except Exception as exc:
        code = getattr(exc, "code", None) or type(exc).__name__
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,96}", code):
            code = "UNEXPECTED_ERROR"
        print(json.dumps({"error": {"code": code, "message": "操作未完成；请按安装使用手册检查环境与数据。"}}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
