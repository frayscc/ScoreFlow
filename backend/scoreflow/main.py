from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import threading
import uuid
from urllib.parse import urlencode
from datetime import date
from pathlib import Path
from typing import Literal, Optional
from contextlib import asynccontextmanager

import uvicorn
import cv2
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .config import FRONTEND_DIST, data_dir
from .db import Store, parse_student_csv
from .omr.template import generate_template
from .omr.scan import ScanQueue
from .reports import generate_period_report
from .roster import parse_roster_text
from .scorepack import ScorepackError, export_scorepack, restore_scorepack


@asynccontextmanager
async def lifespan(_: FastAPI):
    app.state.store = Store(data_dir() / "scoreflow.sqlite3")
    app.state.scan_queue = ScanQueue(app.state.store, data_dir())
    try:
        yield
    finally:
        app.state.scan_queue.shutdown()
        app.state.store.close()


SESSION_TOKEN = os.environ.get("SCOREFLOW_SESSION_TOKEN") or secrets.token_urlsafe(32)
SHUTDOWN_REQUESTED = threading.Event()


class LocalRequestGuard(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "").split(":", 1)[0].lower()
        if host not in {"127.0.0.1", "localhost", "[::1]"}:
            return HTMLResponse("Host 不受信任", status_code=400)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            expected = request.cookies.get("scoreflow_session")
            csrf = request.headers.get("x-scoreflow-csrf")
            if expected != SESSION_TOKEN or csrf != SESSION_TOKEN:
                return HTMLResponse("本地会话验证失败", status_code=403)
            if origin and not (origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")):
                return HTMLResponse("Origin 不受信任", status_code=403)
        return await call_next(request)


app = FastAPI(title="ScoreFlow local API", version=__version__, lifespan=lifespan)
app.add_middleware(LocalRequestGuard)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/session/bootstrap")
def bootstrap(token: str, project: Optional[str] = None, period: Optional[str] = None) -> RedirectResponse:
    if not secrets.compare_digest(token, SESSION_TOKEN):
        raise HTTPException(status_code=403, detail="会话凭据无效")
    query = {}
    for key, value in (("project", project), ("period", period)):
        if value:
            try:
                query[key] = str(uuid.UUID(value))
            except ValueError:
                raise HTTPException(status_code=422, detail="页面定位参数无效")
    response = RedirectResponse("/" + (f"?{urlencode(query)}" if query else ""), status_code=303)
    response.set_cookie("scoreflow_session", SESSION_TOKEN, httponly=True, samesite="strict")
    return response


@app.get("/api/session")
def session(request: Request) -> dict[str, str]:
    if request.cookies.get("scoreflow_session") != SESSION_TOKEN:
        raise HTTPException(status_code=403, detail="会话无效")
    return {"csrf_token": SESSION_TOKEN}


@app.post("/api/app/quit", status_code=202)
def quit_application() -> dict[str, str]:
    SHUTDOWN_REQUESTED.set()
    return {"status": "shutting_down"}


class ProjectInput(BaseModel):
    class_name: str = Field(min_length=1, max_length=80)
    school_year: str = Field(min_length=1, max_length=30)
    group_count: int = Field(default=7, ge=1, le=20)


class CsvImportInput(BaseModel):
    csv_text: str = Field(min_length=1, max_length=200_000)


class RosterTextInput(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)


class GroupingMemberInput(BaseModel):
    student_id: str
    group_number: Optional[int] = Field(default=None, ge=1, le=20)


class GroupingDraftInput(BaseModel):
    revision: int = Field(ge=0)
    members: list[GroupingMemberInput]
    leaders: dict[str, Optional[str]]


class PeriodInput(BaseModel):
    name: str = Field(min_length=1, max_length=80, description="例如：第1—2周")
    start_date: date
    expected_end_date: date


class StudentUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    group_number: int = Field(ge=1, le=20)
    is_leader: bool = False


class RuleInput(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    side: str
    unit_score: int


class ReviewInput(BaseModel):
    decisions: dict[str, str]


class IdentityInput(BaseModel):
    sheet_id: str
    side: str


class BlankInput(BaseModel):
    side: str


class WithdrawBlankInput(BaseModel):
    side: str
    reason: str


class ScanNoteInput(BaseModel):
    note_text: str = Field(min_length=1, max_length=500)
    student_number: Optional[str] = None
    rule_name: Optional[str] = None


class PostingInput(BaseModel):
    idempotency_key: Optional[str] = Field(default=None, max_length=100)


class ReasonInput(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: Optional[str] = Field(default=None, max_length=100)


class RemoveRunsInput(BaseModel):
    run_ids: list[str] = Field(min_length=1, max_length=200)
    reason: str = Field(default="移除测试导入记录", min_length=1, max_length=500)
    reverse_posted: bool = False
    idempotency_key: Optional[str] = Field(default=None, max_length=100)


class ReportInput(BaseModel):
    variant: Literal["teacher", "display"]
    draft: bool = False


def _store(request: Request) -> Store:
    return request.app.state.store


def _delete_managed_files(paths: list[str]) -> None:
    root = data_dir().resolve()
    for value in paths:
        path = Path(value).resolve()
        if path == root or root not in path.parents:
            raise RuntimeError("拒绝删除数据目录之外的文件")
        path.unlink(missing_ok=True)


@app.get("/api/projects")
def list_projects(request: Request):
    return _store(request).list_projects()


@app.post("/api/projects", status_code=201)
def create_project(payload: ProjectInput, request: Request):
    store = _store(request)
    project_id = store.create_project(payload.class_name, payload.school_year, payload.group_count)
    return {"id": project_id, **payload.model_dump()}


@app.post("/api/projects/{project_id}/scorepack", status_code=201)
def create_scorepack(project_id: str, request: Request):
    try:
        path = export_scorepack(_store(request), data_dir(), project_id)
    except ScorepackError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"filename": path.name, "download_url": f"/api/scorepacks/{path.name}"}


@app.get("/api/scorepacks/{filename}")
def download_scorepack(filename: str):
    if filename != Path(filename).name or not filename.endswith(".scorepack"):
        raise HTTPException(status_code=404, detail="项目包不存在")
    path = data_dir() / "exports" / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="项目包不存在")
    return FileResponse(path, media_type="application/zip", filename=filename)


@app.post("/api/scorepacks/restore", status_code=201)
def restore_project_package(request: Request, package: UploadFile = File(...)):
    try:
        project_id = restore_scorepack(_store(request), data_dir(), package.file, package.filename or "project.scorepack")
    except ScorepackError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    project = next(item for item in _store(request).list_projects() if item["id"] == project_id)
    return {"project": project, "status": "restored"}


@app.get("/api/projects/{project_id}/students")
def list_students(project_id: str, request: Request):
    return _store(request).list_students(project_id)


@app.put("/api/projects/{project_id}/students/{student_id}")
def update_student(project_id: str, student_id: str, payload: StudentUpdate, request: Request):
    try:
        _store(request).update_student(project_id, student_id, **payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "updated"}


@app.get("/api/projects/{project_id}/rules")
def list_rules(project_id: str, request: Request):
    return _store(request).list_rules(project_id)


@app.put("/api/projects/{project_id}/rules")
def replace_rules(project_id: str, payload: list[RuleInput], request: Request):
    try:
        _store(request).replace_rules(project_id, [rule.model_dump() for rule in payload])
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "updated", "count": len(payload)}


@app.post("/api/projects/{project_id}/students/import", status_code=201)
def import_students(project_id: str, payload: CsvImportInput, request: Request):
    try:
        ids = _store(request).import_students(project_id, parse_student_csv(payload.csv_text))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"imported": len(ids)}


def _roster_preview(project_id: str, text: str, store: Store) -> dict[str, object]:
    parsed = parse_roster_text(text)
    preview = store.roster_import_preview(project_id, parsed.rows) if parsed.rows else []
    return {
        "valid": parsed.valid and bool(parsed.rows),
        "count": len(parsed.rows),
        "recognized_49": len(parsed.rows) == 49,
        "rows": preview,
        "errors": parsed.errors,
        "warnings": parsed.warnings,
        "summary": {
            "new": sum(row["status"] == "new" for row in preview),
            "unchanged": sum(row["status"] == "unchanged" for row in preview),
            "name_differences": sum(row["status"] == "name_difference" for row in preview),
        },
    }


@app.post("/api/projects/{project_id}/roster/preview")
def preview_roster_text(project_id: str, payload: RosterTextInput, request: Request):
    try:
        return _roster_preview(project_id, payload.text, _store(request))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/roster/import", status_code=201)
def import_roster_text(project_id: str, payload: RosterTextInput, request: Request):
    store = _store(request)
    try:
        preview = _roster_preview(project_id, payload.text, store)
        if not preview["valid"]:
            raise ValueError("名单存在错误，请先按行修正")
        result = store.import_roster_students(project_id, preview["rows"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {**result, "count": preview["count"], "name_differences_preserved": result["name_differences"]}


@app.post("/api/projects/{project_id}/students/preview")
def preview_students(project_id: str, payload: CsvImportInput, request: Request):
    try:
        rows = parse_student_csv(payload.csv_text)
        project = next((item for item in _store(request).list_projects() if item["id"] == project_id), None)
        if not project:
            raise ValueError("项目不存在")
        seen = set()
        for index, row in enumerate(rows):
            number = str(row["student_number"]).strip()
            group = int(row["group_number"])
            if number in seen:
                raise ValueError(f"名单内学号重复：{number}")
            if not 1 <= group <= project["group_count"]:
                raise ValueError(f"第 {index + 1} 行组号超出 1—{project['group_count']}")
            seen.add(number)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"count": len(rows), "rows": rows}


@app.get("/api/periods/{period_id}/grouping")
def get_grouping(period_id: str, request: Request):
    try:
        return _store(request).grouping_draft(period_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/periods/{period_id}/grouping")
def save_grouping(period_id: str, payload: GroupingDraftInput, request: Request):
    try:
        revision = _store(request).save_grouping_draft(
            period_id, payload.revision, [member.model_dump() for member in payload.members], payload.leaders
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "saved", "revision": revision}


@app.get("/api/projects/{project_id}/periods")
def list_periods(project_id: str, request: Request):
    return _store(request).list_periods(project_id)


@app.post("/api/projects/{project_id}/periods", status_code=201)
def create_period(project_id: str, payload: PeriodInput, request: Request):
    if payload.expected_end_date < payload.start_date:
        raise HTTPException(status_code=422, detail="预计结束日期不能早于开始日期")
    try:
        period_id = _store(request).create_period(project_id, payload.name, payload.start_date.isoformat(), payload.expected_end_date.isoformat())
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc
    return {"id": period_id, **payload.model_dump(), "status": "draft"}


@app.put("/api/periods/{period_id}")
def update_period(period_id: str, payload: PeriodInput, request: Request):
    if payload.expected_end_date < payload.start_date:
        raise HTTPException(status_code=422, detail="预计结束日期不能早于开始日期")
    try:
        _store(request).update_draft_period(period_id, payload.name, payload.start_date.isoformat(), payload.expected_end_date.isoformat())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"id": period_id, **payload.model_dump(), "status": "draft"}


@app.post("/api/periods/{period_id}/start", status_code=201)
def start_period(period_id: str, request: Request):
    store = _store(request)
    try:
        sheet_id = store.start_period(period_id)
        data = store.paper_generation_data(period_id, sheet_id)
        period, sheet = data["period"], data["sheet"]
        output_dir = data_dir() / "projects" / period["project_id"] / "papers"
        pdf_path = output_dir / f"paper-{sheet['sheet_number']:02d}-{sheet_id}.pdf"
        manifest_path = pdf_path.with_suffix(".manifest.json")
        temp_pdf = pdf_path.with_suffix(".tmp.pdf")
        temp_manifest = pdf_path.with_suffix(".tmp.manifest.json")
        front = [r["name"] for r in data["rules"] if r["side"] == "front"]
        back = [r["name"] for r in data["rules"] if r["side"] == "back"]
        manifest = generate_template(temp_pdf, temp_manifest, project_id=period["project_id"], period_id=period_id,
                          sheet_id=sheet_id, sheet_number=sheet["sheet_number"], class_name=period["class_name"],
                          period_name=period["name"], students=data["students"], front_rules=front, back_rules=back)
        temp_pdf.replace(pdf_path)
        temp_manifest.replace(manifest_path)
        store.record_template_version(sheet_id, manifest, "7cf5bd68acf6e5fc6c45d7ba7ce27891976b505fefb95512a23bea5596c295c0")
    except Exception as exc:
        for candidate in (locals().get("temp_pdf"), locals().get("temp_manifest"), locals().get("pdf_path"), locals().get("manifest_path")):
            if candidate:
                candidate.unlink(missing_ok=True)
        if "sheet_id" in locals():
            store.rollback_failed_start(period_id)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"sheet_id": sheet_id, "sheet_number": sheet["sheet_number"], "download_url": f"/api/papers/{sheet_id}/pdf"}


@app.get("/api/periods/{period_id}/papers")
def list_papers(period_id: str, request: Request):
    return _store(request).list_sheets(period_id)


@app.post("/api/periods/{period_id}/settle")
def begin_settlement(period_id: str, request: Request):
    try:
        _store(request).begin_settlement(period_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "settling"}


@app.get("/api/periods/{period_id}/results")
def period_results(period_id: str, request: Request):
    try:
        return _store(request).period_results(period_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/periods/{period_id}/close")
def close_period(period_id: str, request: Request):
    try:
        version = _store(request).close_period(period_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "closed", "result_version": version}


@app.post("/api/periods/{period_id}/reopen")
def reopen_period(period_id: str, payload: ReasonInput, request: Request):
    try:
        _store(request).reopen_period(period_id, payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "settling"}


@app.post("/api/periods/{period_id}/reports", status_code=201)
def create_period_report(period_id: str, payload: ReportInput, request: Request):
    store = _store(request)
    try:
        data = store.period_results(period_id)
        period = data["period"]
        is_draft = period["status"] != "closed"
        if is_draft and not payload.draft:
            raise ValueError("周期尚未关闭，只能生成带水印的草稿预览")
        source_digest = hashlib.sha256(
            json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        version = int(period["result_version"])
        existing = store.find_report_export(period_id, version, payload.variant, is_draft, source_digest)
        if existing and Path(existing["stored_path"]).exists():
            return {"id": existing["id"], "download_url": f"/api/reports/{existing['id']}/pdf", "is_draft": is_draft}
        report_dir = data_dir() / "projects" / period["project_id"] / "reports"
        suffix = "教师存档版" if payload.variant == "teacher" else "教室展示版"
        status_name = "草稿" if is_draft else f"v{version}"
        path = report_dir / f"{period['name']}-{suffix}-{status_name}-{source_digest[:8]}.pdf"
        temporary = path.with_suffix(".tmp.pdf")
        generate_period_report(temporary, data, payload.variant, is_draft)
        temporary.replace(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        export_id = store.register_report_export(period_id, version, payload.variant, is_draft, source_digest, str(path), digest)
        return {"id": export_id, "download_url": f"/api/reports/{export_id}/pdf", "is_draft": is_draft}
    except (ValueError, sqlite3.IntegrityError) as exc:
        if "temporary" in locals():
            temporary.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/reports/{export_id}/pdf")
def download_report(export_id: str, request: Request):
    try:
        export = _store(request).get_report_export(export_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    path = Path(export["stored_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="报告文件缺失")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@app.post("/api/periods/{period_id}/papers", status_code=201)
def create_supplemental_paper(period_id: str, request: Request):
    store = _store(request)
    sheet_id = None
    try:
        sheet_id = store.issue_sheet(period_id)
        data = store.paper_generation_data(period_id, sheet_id)
        period, sheet = data["period"], data["sheet"]
        output_dir = data_dir() / "projects" / period["project_id"] / "papers"
        pdf_path = output_dir / f"paper-{sheet['sheet_number']:02d}-{sheet_id}.pdf"
        manifest_path = pdf_path.with_suffix(".manifest.json")
        temp_pdf, temp_manifest = pdf_path.with_suffix(".tmp.pdf"), pdf_path.with_suffix(".tmp.manifest.json")
        front = [r["name"] for r in data["rules"] if r["side"] == "front"]
        back = [r["name"] for r in data["rules"] if r["side"] == "back"]
        manifest = generate_template(temp_pdf, temp_manifest, project_id=period["project_id"], period_id=period_id,
                          sheet_id=sheet_id, sheet_number=sheet["sheet_number"], class_name=period["class_name"],
                          period_name=period["name"], students=data["students"], front_rules=front, back_rules=back)
        temp_pdf.replace(pdf_path)
        temp_manifest.replace(manifest_path)
        store.record_template_version(sheet_id, manifest, "7cf5bd68acf6e5fc6c45d7ba7ce27891976b505fefb95512a23bea5596c295c0")
    except Exception as exc:
        for candidate in (locals().get("temp_pdf"), locals().get("temp_manifest"), locals().get("pdf_path"), locals().get("manifest_path")):
            if candidate:
                candidate.unlink(missing_ok=True)
        if sheet_id:
            store.remove_failed_sheet(sheet_id)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"sheet_id": sheet_id, "sheet_number": sheet["sheet_number"], "download_url": f"/api/papers/{sheet_id}/pdf"}


@app.post("/api/papers/{sheet_id}/void-unused")
def void_unused_paper(sheet_id: str, request: Request):
    try:
        _store(request).void_unused_sheet(sheet_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "void_unused"}


@app.post("/api/papers/{sheet_id}/post")
def post_paper(sheet_id: str, payload: PostingInput, request: Request):
    try:
        result = _store(request).post_sheet(sheet_id, payload.idempotency_key or str(uuid.uuid4()))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


@app.post("/api/papers/{sheet_id}/reverse")
def reverse_paper(sheet_id: str, payload: ReasonInput, request: Request):
    try:
        result = _store(request).reverse_posting(
            sheet_id, payload.reason, payload.idempotency_key or str(uuid.uuid4())
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


@app.post("/api/projects/{project_id}/scans", status_code=202)
def upload_scans(project_id: str, request: Request, files: list[UploadFile] = File(...)):
    results = []
    for upload in files:
        try:
            results.append(request.app.state.scan_queue.ingest(project_id, upload.filename or "scan", upload.file))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            upload.file.close()
    return {"items": results}


@app.get("/api/scan-jobs/{job_id}")
def scan_job(job_id: str, request: Request):
    try:
        return _store(request).get_scan_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/scan-jobs/{job_id}/cancel")
def cancel_scan_job(job_id: str, request: Request):
    try:
        _store(request).cancel_scan_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "cancelling"}


@app.delete("/api/scan-jobs/{job_id}/record")
def remove_scan_job_record(job_id: str, request: Request):
    try:
        _store(request).remove_scan_job_record(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status":"deleted"}


@app.get("/api/projects/{project_id}/scan-imports")
def scan_import_records(project_id: str, request: Request, include_removed: bool = False):
    return _store(request).list_scan_imports(project_id, include_removed=include_removed)


@app.post("/api/scan-records/removal-preview")
def scan_removal_preview(payload: RemoveRunsInput, request: Request):
    try:
        return _store(request).scan_removal_preview(payload.run_ids)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/scan-records")
def remove_scan_records(payload: RemoveRunsInput, request: Request):
    try:
        result = _store(request).remove_recognition_runs(
            payload.run_ids, payload.reason, reverse_posted=payload.reverse_posted,
            idempotency_prefix=payload.idempotency_key or str(uuid.uuid4()),
        )
        _delete_managed_files(result.pop("paths_to_delete", []))
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/scan-assets/{asset_id}")
def remove_scan_asset(asset_id: str, payload: ReasonInput, request: Request):
    try:
        result = _store(request).request_scan_asset_deletion(asset_id, payload.reason)
        _delete_managed_files(result.pop("paths_to_delete", []))
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/recognition-runs/{run_id}/adoption")
def cancel_recognition_adoption(run_id: str, request: Request):
    try:
        _store(request).cancel_run_adoption(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status":"candidate"}


@app.post("/api/recognition-runs/{run_id}/retry", status_code=202)
def retry_recognition(run_id: str, request: Request):
    try:
        return request.app.state.scan_queue.retry(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/recognition-runs")
def recognition_runs(project_id: str, request: Request):
    rows = _store(request).list_recognition_runs(project_id)
    versions = {}
    for row in rows:
        if row.get("sheet_id") and row.get("side"):
            key = (row["sheet_id"], row["side"]); versions[key] = versions.get(key, 0) + 1
    for row in rows:
        row["quality"] = json.loads(row.pop("quality_json")) if row.get("quality_json") else None
        row["version_conflict"] = bool(row.get("sheet_id") and versions.get((row["sheet_id"], row["side"]), 0) > 1)
    return rows


@app.get("/api/recognition-runs/{run_id}")
def recognition_run(run_id: str, request: Request):
    try:
        row = _store(request).get_run(run_id, include_observations=True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    row["quality"] = json.loads(row.pop("quality_json")) if row.get("quality_json") else None
    for observation in row["observations"]:
        observation["features"] = json.loads(observation.pop("features_json"))
    if row.get("sheet_id") and row.get("side"):
        info = _store(request).paper_download_info(row["sheet_id"])
        manifest_path = data_dir() / "projects" / info["project_id"] / "papers" / f"paper-{info['sheet_number']:02d}-{row['sheet_id']}.manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        slots = {slot["slot_id"]: slot for slot in manifest["sides"][row["side"]]["slots"]}
        students = {student["student_id"]: student for student in manifest["students"]}
        for observation in row["observations"]:
            slot = slots[observation["slot_id"]]; student = students[slot["student_id"]]
            observation.update({"student_number":student["student_number"], "student_name":student["name"],
                                "rule_name":slot["rule_name"], "slot_index":slot["slot_index"] + 1})
        summary = {}
        for observation in row["observations"]:
            key = str(observation["student_number"]); item = summary.setdefault(key, {"student_name":observation["student_name"], "valid_marks":0, "x":0, "review":0})
            value = observation["manual_class"] or observation["auto_class"]
            if value.startswith("slash_"): item["valid_marks"] += 1
            elif value == "x": item["x"] += 1
            elif value == "review": item["review"] += 1
        row["student_summary"] = summary
        effective = {name:0 for name in ("blank","slash_forward","slash_back","x","review")}
        delta = 0
        scores = {rule["index"]:rule["unit_score"] for rule in manifest["sides"][row["side"]]["rules"]}
        for observation in row["observations"]:
            value = observation["manual_class"] or observation["auto_class"]
            effective[value] += 1
            if value.startswith("slash_"): delta += scores[slots[observation["slot_id"]]["rule_index"]]
        row["effective_counts"] = effective; row["effective_delta"] = delta
        row["notes"] = _store(request).list_scan_notes(run_id)
    return row


@app.get("/api/recognition-runs/{run_id}/slot/{slot_id}/image")
def slot_context(run_id: str, slot_id: str, request: Request):
    try:
        run = _store(request).get_run(run_id)
        if not run.get("corrected_path") or not run.get("sheet_id") or not run.get("side"): raise ValueError("校正图尚未生成")
        info = _store(request).paper_download_info(run["sheet_id"])
        manifest_path = data_dir() / "projects" / info["project_id"] / "papers" / f"paper-{info['sheet_number']:02d}-{run['sheet_id']}.manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        slot = next(item for item in manifest["sides"][run["side"]]["slots"] if item["slot_id"] == slot_id)
        image = cv2.imread(run["corrected_path"])
        page = manifest["page"]; roi = slot["roi"]; height,width = image.shape[:2]
        margin = 2.5
        x0=max(0,round((roi["x_mm"]-margin)/page["width_mm"]*width)); x1=min(width,round((roi["x_mm"]+roi["width_mm"]+margin)/page["width_mm"]*width))
        y0=max(0,round((page["height_mm"]-roi["y_mm"]-roi["height_mm"]-margin)/page["height_mm"]*height)); y1=min(height,round((page["height_mm"]-roi["y_mm"]+margin)/page["height_mm"]*height))
        ok, encoded = cv2.imencode(".jpg", image[y0:y1,x0:x1], [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok: raise ValueError("图像编码失败")
        return Response(encoded.tobytes(), media_type="image/jpeg")
    except (ValueError, StopIteration) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/recognition-runs/{run_id}/original")
def original_scan(run_id: str, request: Request):
    try: run = _store(request).get_run(run_id)
    except ValueError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc
    path = Path(run["stored_path"])
    if not path.exists(): raise HTTPException(status_code=404, detail="原始文件缺失")
    return FileResponse(path, filename=run["original_filename"])


@app.get("/api/recognition-runs/{run_id}/corrected")
def corrected_scan(run_id: str, request: Request):
    try:
        run = _store(request).get_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    path = Path(run["corrected_path"]) if run.get("corrected_path") else None
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="校正图尚未生成")
    return FileResponse(path, media_type="image/png", filename=f"校正页-{run_id[:8]}.png")


@app.get("/api/recognition-runs/{run_id}/notes-image")
def notes_image(run_id: str, request: Request):
    try:
        run = _store(request).get_run(run_id)
        if not run.get("corrected_path") or not run.get("sheet_id") or not run.get("side"): raise ValueError("校正图尚未生成")
        info = _store(request).paper_download_info(run["sheet_id"])
        manifest_path = data_dir() / "projects" / info["project_id"] / "papers" / f"paper-{info['sheet_number']:02d}-{run['sheet_id']}.manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")); roi = manifest["sides"][run["side"]]["note_roi"]; page=manifest["page"]
        image=cv2.imread(run["corrected_path"]); height,width=image.shape[:2]
        x0=round(roi["x_mm"]/page["width_mm"]*width);x1=round((roi["x_mm"]+roi["width_mm"])/page["width_mm"]*width)
        y0=round((page["height_mm"]-roi["y_mm"]-roi["height_mm"])/page["height_mm"]*height);y1=round((page["height_mm"]-roi["y_mm"])/page["height_mm"]*height)
        ok,encoded=cv2.imencode(".jpg",image[y0:y1,x0:x1],[cv2.IMWRITE_JPEG_QUALITY,92])
        if not ok: raise ValueError("图像编码失败")
        return Response(encoded.tobytes(),media_type="image/jpeg")
    except ValueError as exc: raise HTTPException(status_code=404,detail=str(exc)) from exc


@app.post("/api/recognition-runs/{run_id}/notes", status_code=201)
def add_scan_note(run_id: str, payload: ScanNoteInput, request: Request):
    try: note_id=_store(request).add_scan_note(run_id, payload.note_text, payload.student_number, payload.rule_name)
    except ValueError as exc: raise HTTPException(status_code=422,detail=str(exc)) from exc
    return {"id":note_id}


@app.put("/api/recognition-runs/{run_id}/review")
def review_run(run_id: str, payload: ReviewInput, request: Request):
    try:
        _store(request).set_manual_observations(run_id, payload.decisions)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "saved", "count": len(payload.decisions)}


@app.post("/api/recognition-runs/{run_id}/adopt")
def adopt_run(run_id: str, request: Request):
    try:
        _store(request).adopt_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "reviewed"}


@app.post("/api/recognition-runs/{run_id}/identity")
def resolve_run_identity(run_id: str, payload: IdentityInput, request: Request):
    try:
        run = _store(request).get_run(run_id)
        sheet_id = _store(request).resolve_sheet_reference(run["project_id"], payload.sheet_id)
        request.app.state.scan_queue.resolve_identity(run_id, sheet_id, payload.side)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "ready"}


@app.post("/api/papers/{sheet_id}/confirm-blank")
def confirm_blank(sheet_id: str, payload: BlankInput, request: Request):
    try:
        _store(request).confirm_blank_side(sheet_id, payload.side)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "confirmed_blank"}


@app.post("/api/papers/{sheet_id}/withdraw-blank")
def withdraw_blank(sheet_id: str, payload: WithdrawBlankInput, request: Request):
    try:
        _store(request).withdraw_blank_side(sheet_id, payload.side, payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "missing"}


@app.get("/api/papers/{sheet_id}/pdf")
def download_paper(sheet_id: str, request: Request):
    store = _store(request)
    try:
        row = store.paper_download_info(sheet_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="纸表不存在")
    path = data_dir() / "projects" / row["project_id"] / "papers" / f"paper-{row['sheet_number']:02d}-{sheet_id}.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="纸表文件缺失")
    return FileResponse(path, media_type="application/pdf", filename=f"ScoreFlow-纸表{row['sheet_number']:02d}.pdf")


if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
else:
    @app.get("/", response_class=HTMLResponse)
    def development_placeholder() -> str:
        return """<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">
        <title>ScoreFlow</title><body><main><h1>ScoreFlow</h1>
        <p>本地服务已启动。请构建前端或启动 Vite 开发服务器。</p></main></body></html>"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1"])
    args = parser.parse_args()
    port = args.port or free_port()
    print(f"ScoreFlow: http://127.0.0.1:{port}/session/bootstrap?token={SESSION_TOKEN}", flush=True)
    uvicorn.run(app, host=args.host, port=port, access_log=False)


if __name__ == "__main__":
    run()
