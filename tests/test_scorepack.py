import io
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from scoreflow.db import Store
from scoreflow.omr.template import generate_template
from scoreflow.scorepack import ScorepackError, export_scorepack, restore_scorepack


def _roster():
    return [
        {"student_number": number, "name": f"学生{number}", "group_number": (number - 1) // 2 + 1, "is_leader": number % 2 == 1}
        for number in range(1, 7)
    ]


def _portable_project(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root / "scoreflow.sqlite3")
    project = store.create_project("二四一五班", "2026—2027", 3)
    store.import_students(project, _roster())
    period = store.create_period(project, "第3周", "2026-09-15", "2026-09-21")
    sheet = store.start_period(period)
    data = store.paper_generation_data(period, sheet)
    papers = root / "projects" / project / "papers"
    pdf = papers / f"paper-01-{sheet}.pdf"
    manifest = papers / f"paper-01-{sheet}.manifest.json"
    front = [rule["name"] for rule in data["rules"] if rule["side"] == "front"]
    back = [rule["name"] for rule in data["rules"] if rule["side"] == "back"]
    generated = generate_template(pdf, manifest, project_id=project, period_id=period, sheet_id=sheet,
                                  sheet_number=1, class_name="二四一五班", period_name="第3周",
                                  students=data["students"], front_rules=front, back_rules=back)
    store.record_template_version(sheet, generated, "7cf5bd68acf6e5fc6c45d7ba7ce27891976b505fefb95512a23bea5596c295c0")
    originals = root / "imports" / "originals"
    originals.mkdir(parents=True)
    original = originals / "evidence.png"
    original.write_bytes(b"original evidence")
    job = store.create_scan_job(project)
    asset, _ = store.create_scan_asset(project, "课堂 扫描.png", str(original), "a" * 64, original.stat().st_size, job)
    run = store.create_recognition_run(asset, 0)
    store.save_observations(run, [{"slot_id":"front-r00-c00-s0", "classification":"slash_forward", "confidence":.99, "features":{}}])
    corrected_dir = root / "imports" / "corrected"
    corrected_dir.mkdir(parents=True)
    corrected = corrected_dir / f"{run}.png"
    corrected.write_bytes(b"corrected evidence")
    store.update_recognition_run(run, status="ready", sheet_id=sheet, side="front", corrected_path=str(corrected))
    store.mark_side_candidate(sheet, "front")
    store.adopt_run(run)
    store.confirm_blank_side(sheet, "back")
    store.begin_settlement(period)
    store.post_sheet(sheet, "first-post")
    store.reverse_posting(sheet, "测试恢复关联反向流水", "reverse-post")
    store.post_sheet(sheet, "final-post")
    reports = root / "projects" / project / "reports"
    reports.mkdir(parents=True)
    report = reports / "教师 存档.pdf"
    report.write_bytes(b"%PDF-1.4 portable report")
    store.register_report_export(period, 0, "teacher", True, "b" * 64, str(report), hashlib.sha256(report.read_bytes()).hexdigest())
    return store, project, period, sheet


def test_scorepack_round_trip_preserves_identity_files_and_posting_dedupe(tmp_path):
    source_root = tmp_path / "来源 班级"
    source, project, period, sheet = _portable_project(source_root)
    other = source.create_project("不应进入包", "2026", 1)
    package = export_scorepack(source, source_root, project)
    assert package.suffix == ".scorepack"
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["project_id"] == project
        assert "database/project.sqlite3" in {item["path"] for item in manifest["files"]}
        extracted = tmp_path / "snapshot.sqlite3"
        extracted.write_bytes(archive.read("database/project.sqlite3"))
    snapshot = Store(extracted)
    assert [item["id"] for item in snapshot.list_projects()] == [project]
    assert other not in {item["id"] for item in snapshot.list_projects()}
    snapshot.close()

    destination_root = tmp_path / "恢复 目录"
    destination = Store(destination_root / "scoreflow.sqlite3")
    with package.open("rb") as stream:
        assert restore_scorepack(destination, destination_root, stream, "中文 项目.scorepack") == project
    assert destination.period_results(period)["students"][0]["total_score"] == 105
    assert (destination_root / "projects" / project / "papers" / f"paper-01-{sheet}.pdf").is_file()
    assert (destination_root / "projects" / project / "reports" / "教师 存档.pdf").is_file()
    before = destination.connection.execute("SELECT count(*) FROM posting_batches").fetchone()[0]
    destination.post_sheet(sheet, "second-click")
    assert destination.connection.execute("SELECT count(*) FROM posting_batches").fetchone()[0] == before


def test_bad_hash_and_duplicate_project_never_overwrite_current_data(tmp_path):
    source_root = tmp_path / "source"
    source, project, _, _ = _portable_project(source_root)
    package = export_scorepack(source, source_root, project)
    destination_root = tmp_path / "destination"
    destination = Store(destination_root / "scoreflow.sqlite3")
    existing = destination.create_project("现有项目", "2026", 1)

    tampered = tmp_path / "tampered.scorepack"
    with zipfile.ZipFile(package) as original, zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as changed:
        for info in original.infolist():
            data = original.read(info.filename)
            if info.filename == "config/project.json":
                data += b"x"
            changed.writestr(info, data)
    with tampered.open("rb") as stream, pytest.raises(ScorepackError, match="校验失败"):
        restore_scorepack(destination, destination_root, stream, tampered.name)
    assert [item["id"] for item in destination.list_projects()] == [existing]
    assert list((destination_root / "imports" / "scorepacks" / "failed").glob("*.reason.json"))

    with package.open("rb") as stream:
        restore_scorepack(destination, destination_root, stream, package.name)
    with package.open("rb") as stream, pytest.raises(ScorepackError, match="已存在"):
        restore_scorepack(destination, destination_root, stream, package.name)
    assert len(destination.list_projects()) == 2


def test_zip_slip_is_rejected_before_extraction(tmp_path):
    package = tmp_path / "unsafe.scorepack"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr("../outside.txt", "bad")
    root = tmp_path / "data"
    store = Store(root / "scoreflow.sqlite3")
    with package.open("rb") as stream, pytest.raises(ScorepackError, match="不安全"):
        restore_scorepack(store, root, stream, package.name)
    assert not (tmp_path / "outside.txt").exists()
