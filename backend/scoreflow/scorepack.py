from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unicodedata
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from . import __version__
from .db import SCHEMA_VERSION, Store


FORMAT = "scoreflow-project"
FORMAT_VERSION = 1
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
MAX_FILE_COUNT = 10_000
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 512 * 1024 * 1024
MAX_COMPRESSION_RATIO = 500
MAX_MANIFEST_BYTES = 10 * 1024 * 1024


class ScorepackError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_exists(store: Store, project_id: str) -> bool:
    with store.transaction() as connection:
        return connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is not None


def _prune_snapshot(path: Path, project_id: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=OFF")
        if not connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            raise ScorepackError("项目不存在")
        connection.executescript("""
            CREATE TEMP TABLE keep_ids(id TEXT PRIMARY KEY);
        """)
        id_queries = [
            ("SELECT id FROM projects WHERE id=?", (project_id,)),
            ("SELECT id FROM students WHERE project_id=?", (project_id,)),
            ("SELECT id FROM rules WHERE project_id=?", (project_id,)),
            ("SELECT id FROM periods WHERE project_id=?", (project_id,)),
            ("SELECT id FROM scan_jobs WHERE project_id=?", (project_id,)),
            ("SELECT id FROM scan_assets WHERE project_id=?", (project_id,)),
            ("SELECT ps.id FROM paper_sheets ps JOIN periods p ON p.id=ps.period_id WHERE p.project_id=?", (project_id,)),
            ("SELECT rr.id FROM recognition_runs rr JOIN scan_assets sa ON sa.id=rr.scan_asset_id WHERE sa.project_id=?", (project_id,)),
            ("SELECT pb.id FROM posting_batches pb JOIN paper_sheets ps ON ps.id=pb.sheet_id JOIN periods p ON p.id=ps.period_id WHERE p.project_id=?", (project_id,)),
            ("SELECT re.id FROM report_exports re JOIN periods p ON p.id=re.period_id WHERE p.project_id=?", (project_id,)),
        ]
        for query, parameters in id_queries:
            connection.execute(f"INSERT OR IGNORE INTO keep_ids {query}", parameters)
        statements = [
            ("DELETE FROM report_exports WHERE period_id NOT IN (SELECT id FROM periods WHERE project_id=?)", (project_id,)),
            ("DELETE FROM ledger_entries WHERE batch_id NOT IN (SELECT pb.id FROM posting_batches pb JOIN paper_sheets ps ON ps.id=pb.sheet_id JOIN periods p ON p.id=ps.period_id WHERE p.project_id=?)", (project_id,)),
            ("DELETE FROM posting_batches WHERE sheet_id NOT IN (SELECT ps.id FROM paper_sheets ps JOIN periods p ON p.id=ps.period_id WHERE p.project_id=?)", (project_id,)),
            ("DELETE FROM scan_notes WHERE run_id NOT IN (SELECT rr.id FROM recognition_runs rr JOIN scan_assets sa ON sa.id=rr.scan_asset_id WHERE sa.project_id=?)", (project_id,)),
            ("DELETE FROM slot_observations WHERE run_id NOT IN (SELECT rr.id FROM recognition_runs rr JOIN scan_assets sa ON sa.id=rr.scan_asset_id WHERE sa.project_id=?)", (project_id,)),
            ("DELETE FROM recognition_runs WHERE scan_asset_id NOT IN (SELECT id FROM scan_assets WHERE project_id=?)", (project_id,)),
            ("DELETE FROM scan_assets WHERE project_id<>?", (project_id,)),
            ("DELETE FROM scan_jobs WHERE project_id<>?", (project_id,)),
            ("DELETE FROM paper_sides WHERE sheet_id NOT IN (SELECT ps.id FROM paper_sheets ps JOIN periods p ON p.id=ps.period_id WHERE p.project_id=?)", (project_id,)),
            ("DELETE FROM paper_sheets WHERE period_id NOT IN (SELECT id FROM periods WHERE project_id=?)", (project_id,)),
            ("DELETE FROM period_groups WHERE period_id NOT IN (SELECT id FROM periods WHERE project_id=?)", (project_id,)),
            ("DELETE FROM period_students WHERE period_id NOT IN (SELECT id FROM periods WHERE project_id=?)", (project_id,)),
            ("DELETE FROM period_rules WHERE period_id NOT IN (SELECT id FROM periods WHERE project_id=?)", (project_id,)),
            ("DELETE FROM periods WHERE project_id<>?", (project_id,)),
            ("DELETE FROM class_groups WHERE project_id<>?", (project_id,)),
            ("DELETE FROM students WHERE project_id<>?", (project_id,)),
            ("DELETE FROM rules WHERE project_id<>?", (project_id,)),
            ("DELETE FROM projects WHERE id<>?", (project_id,)),
            ("DELETE FROM template_versions WHERE id NOT IN (SELECT template_id FROM paper_sheets)", ()),
            ("DELETE FROM audit_events WHERE entity_id NOT IN (SELECT id FROM keep_ids)", ()),
        ]
        for statement, parameters in statements:
            connection.execute(statement, parameters)
        # Machine-specific absolute paths never enter a portable package.
        for row in connection.execute("SELECT id,stored_path FROM scan_assets"):
            connection.execute("UPDATE scan_assets SET stored_path=? WHERE id=?", (f"evidence/originals/{Path(row['stored_path']).name}", row["id"]))
        for row in connection.execute("SELECT id,corrected_path FROM recognition_runs WHERE corrected_path IS NOT NULL"):
            connection.execute("UPDATE recognition_runs SET corrected_path=? WHERE id=?", (f"evidence/corrected/{Path(row['corrected_path']).name}", row["id"]))
        for row in connection.execute("SELECT id,stored_path FROM report_exports"):
            connection.execute("UPDATE report_exports SET stored_path=? WHERE id=?", (f"project/reports/{Path(row['stored_path']).name}", row["id"]))
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ScorepackError("项目数据库快照完整性检查失败")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ScorepackError("项目数据库快照关联检查失败")
        connection.execute("VACUUM")
    finally:
        connection.close()


def _copy_payload(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise ScorepackError(f"项目引用文件缺失：{source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def export_scorepack(store: Store, root: Path, project_id: str) -> Path:
    if not _project_exists(store, project_id):
        raise ScorepackError("项目不存在")
    export_dir = root / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="scoreflow-export-", dir=root) as temporary_name:
        stage = Path(temporary_name)
        database = stage / "database" / "project.sqlite3"
        store.backup_to(database)
        _prune_snapshot(database, project_id)

        project_dir = root / "projects" / project_id
        if project_dir.exists():
            for source in sorted(p for p in project_dir.rglob("*") if p.is_file()):
                _copy_payload(source, stage / "project" / source.relative_to(project_dir))

        snapshot = sqlite3.connect(database)
        snapshot.row_factory = sqlite3.Row
        try:
            for row in snapshot.execute("SELECT stored_path FROM scan_assets"):
                portable = PurePosixPath(row["stored_path"])
                _copy_payload(root / "imports" / "originals" / portable.name, stage / Path(*portable.parts))
            for row in snapshot.execute("SELECT corrected_path FROM recognition_runs WHERE corrected_path IS NOT NULL"):
                portable = PurePosixPath(row["corrected_path"])
                _copy_payload(root / "imports" / "corrected" / portable.name, stage / Path(*portable.parts))
            project = dict(snapshot.execute("SELECT id,class_name,school_year,group_count,created_at FROM projects").fetchone())
        finally:
            snapshot.close()
        config_path = stage / "config" / "project.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")

        files = []
        for path in sorted(p for p in stage.rglob("*") if p.is_file()):
            relative = path.relative_to(stage).as_posix()
            files.append({"path": relative, "size": path.stat().st_size, "sha256": _sha256(path)})
        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "database_version": SCHEMA_VERSION,
            "application_version": __version__,
            "project_id": project_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "files": files,
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        final_path = export_dir / f"ScoreFlow-{project_id[:8]}-{stamp}.scorepack"
        temporary_path = export_dir / f".{final_path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with zipfile.ZipFile(temporary_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for path in sorted(p for p in stage.rglob("*") if p.is_file()):
                    archive.write(path, path.relative_to(stage).as_posix())
            temporary_path.replace(final_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    return final_path


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if not members or len(members) > MAX_FILE_COUNT:
        raise ScorepackError("项目包文件数量异常")
    total = 0
    seen: set[str] = set()
    portable_seen: set[str] = set()
    for info in members:
        path = PurePosixPath(info.filename)
        portable_key = unicodedata.normalize("NFC", info.filename).casefold()
        if (info.filename in seen or portable_key in portable_seen or path.is_absolute() or ".." in path.parts
                or "\\" in info.filename or any(":" in part for part in path.parts) or not path.parts):
            raise ScorepackError("项目包包含不安全或重复路径")
        seen.add(info.filename)
        portable_seen.add(portable_key)
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ScorepackError("项目包不允许符号链接")
        if info.file_size > MAX_SINGLE_FILE_BYTES:
            raise ScorepackError("项目包内单个文件过大")
        total += info.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise ScorepackError("项目包解压后体积过大")
        if info.file_size and info.compress_size == 0:
            raise ScorepackError("项目包压缩信息异常")
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            raise ScorepackError("项目包压缩比异常")
    return members


def _validate_and_extract(package: Path, stage: Path) -> dict:
    try:
        with zipfile.ZipFile(package) as archive:
            members = _safe_members(archive)
            by_name = {m.filename: m for m in members}
            if "manifest.json" not in by_name:
                raise ScorepackError("项目包缺少 manifest.json")
            if by_name["manifest.json"].file_size > MAX_MANIFEST_BYTES:
                raise ScorepackError("项目包 manifest 过大")
            try:
                manifest = json.loads(archive.read("manifest.json"))
            except (json.JSONDecodeError, KeyError) as exc:
                raise ScorepackError("项目包 manifest 无效") from exc
            if manifest.get("format") != FORMAT or manifest.get("format_version") != FORMAT_VERSION:
                raise ScorepackError("项目包格式版本不受支持")
            if not isinstance(manifest.get("project_id"), str):
                raise ScorepackError("项目包缺少项目身份")
            if manifest.get("database_version", 0) > SCHEMA_VERSION:
                raise ScorepackError("项目包数据库版本高于当前程序")
            declared = manifest.get("files")
            if not isinstance(declared, list):
                raise ScorepackError("项目包文件清单无效")
            actual = {m.filename for m in members if not m.is_dir() and m.filename != "manifest.json"}
            expected = {item.get("path") for item in declared if isinstance(item, dict)}
            if len(expected) != len(declared) or actual != expected:
                raise ScorepackError("项目包文件清单与内容不一致")
            for item in declared:
                path = PurePosixPath(item["path"])
                target = stage / Path(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest, size = hashlib.sha256(), 0
                with archive.open(item["path"]) as source, target.open("wb") as output:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        size += len(chunk); digest.update(chunk); output.write(chunk)
                if size != item.get("size") or digest.hexdigest() != item.get("sha256"):
                    raise ScorepackError(f"项目包文件校验失败：{item['path']}")
            return manifest
    except (zipfile.BadZipFile, OSError) as exc:
        raise ScorepackError("文件不是有效的 scorepack 项目包") from exc


def _validate_database(database: Path, manifest: dict) -> None:
    if not database.is_file():
        raise ScorepackError("项目包缺少数据库快照")
    try:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ScorepackError("项目包数据库已损坏")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ScorepackError("项目包数据库关联无效")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION or version != manifest["database_version"]:
            raise ScorepackError("项目包数据库版本不匹配")
        if version < SCHEMA_VERSION:
            connection.close()
            migrated = Store(database)
            migrated.close()
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise ScorepackError("项目包数据库升级失败")
        projects = connection.execute("SELECT id FROM projects").fetchall()
        if len(projects) != 1 or projects[0]["id"] != manifest["project_id"]:
            raise ScorepackError("项目包项目身份不一致")
        for row in connection.execute("SELECT stored_path FROM scan_assets"):
            if not (database.parents[1] / Path(*PurePosixPath(row["stored_path"]).parts)).is_file():
                raise ScorepackError("项目包缺少原始扫描证据")
        for row in connection.execute("SELECT corrected_path FROM recognition_runs WHERE corrected_path IS NOT NULL"):
            if not (database.parents[1] / Path(*PurePosixPath(row["corrected_path"]).parts)).is_file():
                raise ScorepackError("项目包缺少校正证据")
        for row in connection.execute("SELECT stored_path FROM report_exports"):
            if not (database.parents[1] / Path(*PurePosixPath(row["stored_path"]).parts)).is_file():
                raise ScorepackError("项目包缺少报告文件")
        for row in connection.execute("SELECT ps.id,ps.sheet_number FROM paper_sheets ps"):
            base = database.parents[1] / "project" / "papers" / f"paper-{row['sheet_number']:02d}-{row['id']}"
            if not base.with_suffix(".pdf").is_file() or not Path(f"{base}.manifest.json").is_file():
                raise ScorepackError("项目包缺少已签发纸表或版式 manifest")
    except sqlite3.DatabaseError as exc:
        raise ScorepackError("项目包数据库无法读取") from exc
    finally:
        if "connection" in locals():
            connection.close()


TABLE_ORDER = [
    "projects", "students", "class_groups", "rules", "periods", "period_students", "period_groups",
    "period_rules", "template_versions", "paper_sheets", "paper_sides", "scan_jobs", "scan_assets",
    "recognition_runs", "slot_observations", "scan_notes", "posting_batches", "ledger_entries", "report_exports",
]


def _insert_snapshot(store: Store, database: Path, root: Path) -> str:
    source = sqlite3.connect(database)
    source.row_factory = sqlite3.Row
    try:
        project_id = source.execute("SELECT id FROM projects").fetchone()[0]
        with store.transaction() as target:
            target.execute("PRAGMA defer_foreign_keys=ON")
            if target.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise ScorepackError("当前数据中已存在同一项目；为防覆盖已拒绝恢复")
            for table in TABLE_ORDER:
                columns = [row[1] for row in target.execute(f"PRAGMA table_info({table})")]
                source_columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})")]
                if source_columns != columns:
                    raise ScorepackError(f"项目包数据库表结构不兼容：{table}")
                placeholders = ",".join("?" for _ in columns)
                names = ",".join(f'"{column}"' for column in columns)
                rows = [tuple(row[column] for column in columns) for row in source.execute(f"SELECT {names} FROM {table}")]
                if table == "template_versions":
                    for row in rows:
                        existing = target.execute("SELECT geometry_json,font_sha256 FROM template_versions WHERE id=?", (row[0],)).fetchone()
                        if existing and (existing[0] != row[2] or existing[1] != row[3]):
                            raise ScorepackError("模板版本身份冲突")
                    verb = "INSERT OR IGNORE"
                else:
                    verb = "INSERT"
                if rows:
                    target.executemany(f"{verb} INTO {table}({names}) VALUES({placeholders})", rows)
            # Audit IDs are local bookkeeping identities, so allocate fresh IDs.
            audits = source.execute("SELECT action,entity_id,details_json,created_at FROM audit_events ORDER BY id").fetchall()
            target.executemany("INSERT INTO audit_events(action,entity_id,details_json,created_at) VALUES(?,?,?,?)", [tuple(row) for row in audits])
            for row in source.execute("SELECT id,stored_path FROM scan_assets"):
                target.execute("UPDATE scan_assets SET stored_path=? WHERE id=?", (str(root / "imports" / "originals" / PurePosixPath(row["stored_path"]).name), row["id"]))
            for row in source.execute("SELECT id,corrected_path FROM recognition_runs WHERE corrected_path IS NOT NULL"):
                target.execute("UPDATE recognition_runs SET corrected_path=? WHERE id=?", (str(root / "imports" / "corrected" / PurePosixPath(row["corrected_path"]).name), row["id"]))
            for row in source.execute("SELECT id,stored_path FROM report_exports"):
                target.execute("UPDATE report_exports SET stored_path=? WHERE id=?", (str(root / "projects" / project_id / "reports" / PurePosixPath(row["stored_path"]).name), row["id"]))
            if target.execute("PRAGMA foreign_key_check").fetchall():
                raise ScorepackError("恢复后的数据库关联检查失败")
        return project_id
    finally:
        source.close()


def restore_scorepack(store: Store, root: Path, source: BinaryIO, original_name: str = "project.scorepack") -> str:
    incoming = root / "imports" / "scorepacks" / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    package = incoming / f"{uuid.uuid4().hex}-{Path(original_name).name}"
    size = 0
    try:
        with package.open("wb") as output:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                if size > MAX_PACKAGE_BYTES:
                    raise ScorepackError("项目包超过 1GB 限制")
                output.write(chunk)
    except Exception as exc:
        failed = root / "imports" / "scorepacks" / "failed"
        failed.mkdir(parents=True, exist_ok=True)
        failed_package = failed / package.name
        package.replace(failed_package)
        failed_package.with_suffix(failed_package.suffix + ".reason.json").write_text(
            json.dumps({"error": str(exc), "failed_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        raise
    stage_dir = root / "imports" / "scorepacks" / "staging"
    stage_dir.mkdir(parents=True, exist_ok=True)
    project_target: Path | None = None
    created_evidence: list[Path] = []
    try:
        with tempfile.TemporaryDirectory(prefix="restore-", dir=stage_dir) as temporary_name:
            stage = Path(temporary_name)
            manifest = _validate_and_extract(package, stage)
            database = stage / "database" / "project.sqlite3"
            _validate_database(database, manifest)
            project_id = manifest["project_id"]
            if _project_exists(store, project_id):
                raise ScorepackError("当前数据中已存在同一项目；为防覆盖已拒绝恢复")
            project_target = root / "projects" / project_id
            if project_target.exists():
                raise ScorepackError("目标项目目录已存在；为防覆盖已拒绝恢复")
            project_source = stage / "project"
            project_target.parent.mkdir(parents=True, exist_ok=True)
            if project_source.exists():
                shutil.copytree(project_source, project_target)
            else:
                project_target.mkdir()
            for kind in ("originals", "corrected"):
                evidence_source = stage / "evidence" / kind
                evidence_target = root / "imports" / kind
                evidence_target.mkdir(parents=True, exist_ok=True)
                if evidence_source.exists():
                    for item in evidence_source.iterdir():
                        target = evidence_target / item.name
                        if target.exists():
                            if _sha256(target) != _sha256(item):
                                raise ScorepackError("证据文件名冲突")
                        else:
                            shutil.copyfile(item, target); created_evidence.append(target)
            restored_id = _insert_snapshot(store, database, root)
            return restored_id
    except Exception as exc:
        if project_target and project_target.exists():
            shutil.rmtree(project_target)
        for path in created_evidence:
            path.unlink(missing_ok=True)
        failed = root / "imports" / "scorepacks" / "failed"
        failed.mkdir(parents=True, exist_ok=True)
        failed_package = failed / package.name
        package.replace(failed_package)
        reason = failed_package.with_suffix(failed_package.suffix + ".reason.json")
        reason.write_text(json.dumps({"error": str(exc), "failed_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False, indent=2), encoding="utf-8")
        if isinstance(exc, ScorepackError):
            raise
        if isinstance(exc, sqlite3.IntegrityError):
            raise ScorepackError("项目身份与当前数据冲突，未恢复任何内容") from exc
        raise ScorepackError(f"项目包恢复失败：{exc}") from exc
    finally:
        package.unlink(missing_ok=True)
