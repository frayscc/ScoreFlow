from __future__ import annotations

import hashlib
import base64
import json
import queue
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageOps

from ..db import Store
from .recognize import align_with_markers, recognize_aligned


MAX_FILE_BYTES = 250 * 1024 * 1024
MAX_PAGES = 200
MAX_PAGE_PIXELS = 100_000_000
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}


def decode_identity(image: np.ndarray) -> tuple[Optional[dict[str, Any]], np.ndarray, int]:
    rotations = [(image, 0), (cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE), 90),
                 (cv2.rotate(image, cv2.ROTATE_180), 180), (cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE), 270)]
    for oriented, degrees in rotations:
        gray = cv2.cvtColor(oriented, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        payload, points = "", None
        height, width = oriented.shape[:2]
        qr_region = oriented[0:int(height * .34), int(width * .60):width]
        qr_gray = cv2.cvtColor(qr_region, cv2.COLOR_BGR2GRAY)
        _, qr_binary = cv2.threshold(qr_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        candidates = [
            (cv2.resize(qr_region, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), True),
            (cv2.resize(qr_gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), True),
            (cv2.resize(qr_binary, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST), True),
            (oriented, False), (gray, False), (binary, False),
        ]
        trusted_region = False
        for candidate, is_region in candidates:
            payload, points, _ = cv2.QRCodeDetector().detectAndDecode(candidate)
            if payload and points is not None:
                trusted_region = is_region
                break
        if not payload or points is None:
            continue
        if not trusted_region:
            center = points.reshape(-1, 2).mean(axis=0)
            if center[0] <= oriented.shape[1] * 0.6 or center[1] >= oriented.shape[0] * 0.4:
                continue
        if payload.startswith("SF1|"):
            try:
                _, project, period, sheet, face, template, layout_hash = payload.split("|")
                def expand(value: str) -> str:
                    if len(value) != 22: return value
                    return str(uuid.UUID(bytes=base64.urlsafe_b64decode(value + "==")))
                return {"format":"scoreflow-paper-v1", "project_id":expand(project), "period_id":expand(period),
                        "sheet_id":expand(sheet), "side":"front" if face == "F" else "back",
                        "template_id":template, "layout_hash":layout_hash}, oriented, degrees
            except (ValueError, TypeError):
                continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if parsed.get("v") == 1:
            parsed = {"format":"scoreflow-paper-v1", "project_id":parsed.get("p"), "period_id":parsed.get("c"),
                      "sheet_id":parsed.get("s"), "side":"front" if parsed.get("f") == "F" else "back",
                      "template_id":parsed.get("t"), "layout_hash":parsed.get("h")}
        if parsed.get("format") == "scoreflow-paper-v1":
            return parsed, oriented, degrees
    return None, image, 0


def quality_metrics(gray: np.ndarray) -> dict[str, Any]:
    height, width = gray.shape
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())
    problems = []
    if min(width, height) < 1600:
        problems.append("分辨率偏低，建议使用300dpi扫描")
    if blur < 35:
        problems.append("图像可能模糊")
    if brightness < 120 or brightness > 252:
        problems.append("曝光异常")
    return {"width": width, "height": height, "blur_score": round(blur, 2), "mean_brightness": round(brightness, 2), "problems": problems}


def pages_from_file(path: Path):
    if path.suffix.lower() == ".pdf":
        document = pdfium.PdfDocument(path)
        if len(document) > MAX_PAGES: raise ValueError(f"PDF超过{MAX_PAGES}页限制")
        for index in range(len(document)):
            page_width, page_height = document[index].get_size()
            if page_width * page_height * (300 / 72) ** 2 > MAX_PAGE_PIXELS: raise ValueError("PDF页面尺寸异常")
            # Electronic-ink apps usually store pen strokes as PDF annotations.
            # Keep annotations enabled explicitly so PDFium behavior cannot change
            # silently across dependency upgrades.
            yield index, cv2.cvtColor(np.asarray(document[index].render(
                scale=300 / 72, draw_annots=True, optimize_mode="print"
            ).to_pil().convert("RGB")), cv2.COLOR_RGB2BGR)
    else:
        with Image.open(path) as source:
            if source.width * source.height > MAX_PAGE_PIXELS: raise ValueError("图片像素尺寸超过限制")
            rgb = ImageOps.exif_transpose(source).convert("RGB")
            yield 0, cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2BGR)


class ScanQueue:
    def __init__(self, store: Store, root: Path, capacity: int = 4):
        self.store, self.root = store, root
        self.store.recover_interrupted_jobs()
        self.pending: queue.Queue[Any] = queue.Queue(maxsize=capacity)
        self.thread = threading.Thread(target=self._worker, name="scoreflow-omr", daemon=True)
        self.thread.start()

    def ingest(self, project_id: str, filename: str, source) -> dict[str, Any]:
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise ValueError("仅支持 PDF、JPG、JPEG 和 PNG")
        job_id = self.store.create_scan_job(project_id, filename)
        temp_dir = self.root / "imports" / "tmp"
        asset_dir = self.root / "imports" / "originals"
        temp_dir.mkdir(parents=True, exist_ok=True); asset_dir.mkdir(parents=True, exist_ok=True)
        temp_path = temp_dir / f"{job_id}{suffix}"
        digest, size = hashlib.sha256(), 0
        with temp_path.open("wb") as output:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk: break
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    output.close(); temp_path.unlink(missing_ok=True)
                    self.store.update_scan_job(job_id, status="failed", stage="导入失败", error="文件超过250MB")
                    raise ValueError("文件超过250MB限制")
                digest.update(chunk); output.write(chunk)
        sha = digest.hexdigest(); final_path = asset_dir / f"{sha}{suffix}"
        asset_id, duplicate = self.store.create_scan_asset(project_id, filename, str(final_path), sha, size, job_id)
        if duplicate:
            temp_path.unlink(missing_ok=True)
            self.store.update_scan_job(job_id, status="completed", stage="重复文件", total_pages=0, completed_pages=0)
            return {"job_id": job_id, "asset_id": asset_id, "duplicate": True}
        temp_path.replace(final_path)
        try:
            self.pending.put_nowait((job_id, asset_id, final_path))
        except queue.Full as exc:
            self.store.remove_unprocessed_asset(asset_id)
            final_path.unlink(missing_ok=True)
            self.store.update_scan_job(job_id, status="failed", stage="队列已满", error="请等待现有任务完成")
            raise ValueError("处理队列已满，请稍后重试") from exc
        return {"job_id": job_id, "asset_id": asset_id, "duplicate": False}

    def _worker(self):
        while True:
            item = self.pending.get()
            if item is None:
                self.pending.task_done(); return
            try:
                if item[0] == "retry":
                    _, job_id, asset_id, path, run_id, page_index = item
                    self._process_retry(job_id, asset_id, path, run_id, page_index)
                else:
                    job_id, asset_id, path = item
                    self._process(job_id, asset_id, path)
            except Exception as exc:
                self.store.update_scan_job(job_id, status="failed", stage="处理失败", error=str(exc))
            finally:
                self.pending.task_done()

    @staticmethod
    def _unlink_paths(paths: list[str]) -> None:
        for path in paths:
            Path(path).unlink(missing_ok=True)

    def _finalize_requested_deletion(self, job_id: str, asset_id: str, completed_pages: int) -> bool:
        if not self.store.scan_asset_delete_requested(asset_id):
            return False
        result = self.store.finalize_requested_asset_deletion(asset_id)
        self._unlink_paths(result.get("paths_to_delete", []))
        self.store.update_scan_job(job_id, status="cancelled", stage="已取消并删除", completed_pages=completed_pages)
        return True

    def shutdown(self) -> None:
        # Ask queued/running jobs to stop at the next safe page boundary. The
        # worker still drains its queue so every job reaches a durable state.
        self.store.cancel_active_scan_jobs()
        self.pending.join()
        self.pending.put(None)
        self.thread.join(timeout=10)

    def retry(self, run_id: str) -> dict[str, Any]:
        info = self.store.prepare_recognition_retry(run_id)
        if info.get("old_corrected_path"):
            Path(str(info["old_corrected_path"])).unlink(missing_ok=True)
        try:
            self.pending.put_nowait(("retry", info["job_id"], info["asset_id"], Path(str(info["stored_path"])), run_id, info["page_index"]))
        except queue.Full as exc:
            self.store.update_recognition_run(run_id, status="failed", error="处理队列已满，请稍后重试")
            self.store.update_scan_job(str(info["job_id"]), status="failed", stage="队列已满", error="请等待现有任务完成")
            raise ValueError("处理队列已满，请稍后重试") from exc
        return {"status":"queued", "job_id":info["job_id"], "run_id":run_id}

    def _process_retry(self, job_id: str, asset_id: str, path: Path, run_id: str, page_index: int) -> None:
        self.store.update_scan_job(job_id, status="processing", stage="重新识别页面")
        page_image = next((image for index, image in pages_from_file(path) if index == page_index), None)
        if page_image is None:
            raise ValueError("找不到要重新识别的原始页")
        if self._recognize_page(job_id, asset_id, run_id, page_index, page_image):
            return
        self.store.update_scan_job(job_id, status="completed", stage="等待复核")

    def _process(self, job_id: str, asset_id: str, path: Path):
        self.store.update_scan_job(job_id, status="processing", stage="栅格化")
        if self._finalize_requested_deletion(job_id, asset_id, 0):
            return
        pages = list(pages_from_file(path)) if path.suffix.lower() != ".pdf" else pages_from_file(path)
        total = len(pdfium.PdfDocument(path)) if path.suffix.lower() == ".pdf" else 1
        if total > MAX_PAGES: raise ValueError(f"PDF超过{MAX_PAGES}页限制")
        self.store.update_scan_job(job_id, status="processing", stage="识别页面", total_pages=total)
        for completed, (page_index, image) in enumerate(pages, start=1):
            if self.store.get_scan_job(job_id)["cancel_requested"]:
                if self._finalize_requested_deletion(job_id, asset_id, completed - 1):
                    return
                self.store.update_scan_job(job_id, status="cancelled", stage="已取消", completed_pages=completed - 1)
                return
            run_id = self.store.create_recognition_run(asset_id, page_index)
            if self._recognize_page(job_id, asset_id, run_id, page_index, image, completed):
                return
            self.store.update_scan_job(job_id, status="processing", stage="识别页面", completed_pages=completed)
            if self._finalize_requested_deletion(job_id, asset_id, completed):
                return
        self.store.update_scan_job(job_id, status="completed", stage="等待复核", completed_pages=total)

    def _recognize_page(self, job_id: str, asset_id: str, run_id: str, page_index: int,
                        image: np.ndarray, completed: int = 0) -> bool:
        self.store.update_recognition_run(run_id, status="processing")
        identity, oriented, rotation = decode_identity(image)
        if not identity:
            self.store.update_recognition_run(run_id, status="needs_identity", quality={"rotation": rotation, **quality_metrics(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))})
            self.store.update_scan_job(job_id, status="processing", stage="需要确认页面身份", completed_pages=completed or None)
            return self._finalize_requested_deletion(job_id, asset_id, max(0, completed))
        asset = self.store.get_run(run_id)
        if identity.get("project_id") != asset["project_id"]:
            self.store.update_recognition_run(run_id, status="failed", error="页面属于其他项目")
            self.store.update_scan_job(job_id, status="processing", stage="发现错误项目页面", completed_pages=completed or None)
            return self._finalize_requested_deletion(job_id, asset_id, max(0, completed))
        sheet_id, side = identity.get("sheet_id"), identity.get("side")
        try:
            info = self.store.paper_download_info(sheet_id)
            if info["project_id"] != asset["project_id"]: raise ValueError("页面属于其他项目")
            manifest_path = self.root / "projects" / info["project_id"] / "papers" / f"paper-{info['sheet_number']:02d}-{sheet_id}.manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if identity.get("period_id") != info["period_id"] or identity.get("layout_hash") != manifest["layout_hash"][:16]:
                raise ValueError("页面周期或模板版本不匹配")
            aligned = align_with_markers(oriented, manifest, side)
            corrected_dir = self.root / "imports" / "corrected"; corrected_dir.mkdir(parents=True, exist_ok=True)
            corrected_path = corrected_dir / f"{run_id}.png"
            cv2.imwrite(str(corrected_path), aligned)
            result = recognize_aligned(corrected_path, manifest_path, side)
            if self.store.scan_asset_delete_requested(asset_id):
                corrected_path.unlink(missing_ok=True)
                return self._finalize_requested_deletion(job_id, asset_id, max(0, completed - 1))
            slots = {slot["slot_id"]: slot for slot in manifest["sides"][side]["slots"]}
            scores = {rule["index"]: rule["unit_score"] for rule in manifest["sides"][side]["rules"]}
            predicted_delta = sum(scores[slots[item["slot_id"]]["rule_index"]] for item in result["observations"] if item["classification"].startswith("slash_"))
            quality = {"rotation": rotation, **quality_metrics(cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)), "counts": result["counts"], "predicted_delta":predicted_delta}
            self.store.save_observations(run_id, result["observations"])
            previous_side_status = self.store.mark_side_candidate(sheet_id, side)
            if previous_side_status == "confirmed_blank" and sum(value for key,value in result["counts"].items() if key != "blank"):
                quality["problems"].append("与此前的空白面确认冲突")
            self.store.update_recognition_run(run_id, status="ready", sheet_id=sheet_id, side=side, corrected_path=str(corrected_path), quality=quality)
        except Exception as exc:
            self.store.update_recognition_run(run_id, status="failed", sheet_id=sheet_id, side=side, error=str(exc))
        return self._finalize_requested_deletion(job_id, asset_id, max(0, completed))

    def resolve_identity(self, run_id: str, sheet_id: str, side: str) -> None:
        if side not in {"front", "back"}: raise ValueError("面别无效")
        run = self.store.get_run(run_id)
        if run["status"] != "needs_identity": raise ValueError("该页面不需要人工确认身份")
        info = self.store.paper_download_info(sheet_id)
        if info["project_id"] != run["project_id"]: raise ValueError("纸表属于其他项目")
        page_image = next((image for index, image in pages_from_file(Path(run["stored_path"])) if index == run["page_index"]), None)
        if page_image is None: raise ValueError("找不到原始页")
        detector = cv2.QRCodeDetector(); oriented = page_image
        for candidate in (page_image, cv2.rotate(page_image, cv2.ROTATE_90_CLOCKWISE), cv2.rotate(page_image, cv2.ROTATE_180), cv2.rotate(page_image, cv2.ROTATE_90_COUNTERCLOCKWISE)):
            ok, points = detector.detect(candidate)
            if ok and points is not None and points.reshape(-1,2).mean(axis=0)[0] > candidate.shape[1] * .6 and points.reshape(-1,2).mean(axis=0)[1] < candidate.shape[0] * .4:
                oriented = candidate; break
        manifest_path = self.root / "projects" / info["project_id"] / "papers" / f"paper-{info['sheet_number']:02d}-{sheet_id}.manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        aligned = align_with_markers(oriented, manifest, side)
        corrected_dir = self.root / "imports" / "corrected"; corrected_dir.mkdir(parents=True, exist_ok=True)
        corrected_path = corrected_dir / f"{run_id}.png"; cv2.imwrite(str(corrected_path), aligned)
        result = recognize_aligned(corrected_path, manifest_path, side)
        self.store.save_observations(run_id, result["observations"])
        previous_side_status = self.store.mark_side_candidate(sheet_id, side)
        slots = {slot["slot_id"]: slot for slot in manifest["sides"][side]["slots"]}; scores = {rule["index"]:rule["unit_score"] for rule in manifest["sides"][side]["rules"]}
        predicted_delta = sum(scores[slots[item["slot_id"]]["rule_index"]] for item in result["observations"] if item["classification"].startswith("slash_"))
        quality = {**quality_metrics(cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)), "counts":result["counts"], "predicted_delta":predicted_delta, "identity_source":"manual"}
        if previous_side_status == "confirmed_blank" and sum(value for key,value in result["counts"].items() if key != "blank"):
            quality["problems"].append("与此前的空白面确认冲突")
        self.store.update_recognition_run(run_id, status="ready", sheet_id=sheet_id, side=side, corrected_path=str(corrected_path), quality=quality)
