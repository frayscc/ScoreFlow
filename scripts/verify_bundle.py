"""Launch an onedir binary with a clean environment and verify the local UI."""
from __future__ import annotations

import json
import http.cookiejar
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("用法：python scripts/verify_bundle.py <ScoreFlow可执行文件>")
    supplied = Path(sys.argv[1]).resolve()
    if not supplied.exists():
        raise SystemExit(f"应用或可执行文件不存在：{supplied}")
    with tempfile.TemporaryDirectory(prefix="ScoreFlow 中文 空格 ") as name:
        if supplied.is_dir() and supplied.suffix == ".app":
            copied_app = Path(name) / "应用 目录" / "ScoreFlow 应用.app"
            copied_app.parent.mkdir()
            shutil.copytree(supplied, copied_app, symlinks=True)
            executable = copied_app / "Contents" / "MacOS" / "ScoreFlow"
        else:
            executable = supplied
        if not executable.is_file():
            raise RuntimeError(f"应用内缺少可执行文件：{executable}")
        data = Path(name) / "数据 目录"
        env = {
            "PATH": "/usr/bin:/bin" if os.name != "nt" else os.environ.get("SystemRoot", r"C:\Windows") + r"\System32",
            "SCOREFLOW_DATA_DIR": str(data),
            "SCOREFLOW_NO_BROWSER": "1",
            "LANG": "zh_CN.UTF-8",
        }
        process = subprocess.Popen([str(executable)], env=env)
        try:
            state_path = data / ".runtime" / "instance.json"
            deadline = time.time() + 40
            while time.time() < deadline and not state_path.exists():
                if process.poll() is not None:
                    raise RuntimeError(f"程序提前退出：{process.returncode}")
                time.sleep(.1)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            base = f"http://127.0.0.1:{state['port']}"
            cookies = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
            opener.open(base + f"/session/bootstrap?token={state['token']}", timeout=3)
            with opener.open(base + "/api/health", timeout=3) as response:
                assert json.load(response)["status"] == "ok"
            with opener.open(base + "/", timeout=3) as response:
                html = response.read().decode("utf-8")
                assert "ScoreFlow" in html and "http://" not in html and "https://" not in html
            csrf = json.load(opener.open(base + "/api/session", timeout=3))["csrf_token"]

            def post(path: str, payload: dict):
                request = urllib.request.Request(
                    base + path,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json", "X-ScoreFlow-CSRF": csrf},
                    method="POST",
                )
                return json.load(opener.open(request, timeout=30))

            project = post("/api/projects", {"class_name":"便携包验证班", "school_year":"2026—2027", "group_count":2})
            roster = "学号,姓名,组号,是否组长\n1,甲同学,1,是\n2,乙同学,2,是"
            post(f"/api/projects/{project['id']}/students/import", {"csv_text":roster})
            period = post(f"/api/projects/{project['id']}/periods", {"name":"第1周", "start_date":"2026-09-01", "expected_end_date":"2026-09-07"})
            paper = post(f"/api/periods/{period['id']}/start", {})
            with opener.open(base + paper["download_url"], timeout=30) as response:
                assert response.read(4) == b"%PDF"
            exported = post(f"/api/projects/{project['id']}/scorepack", {})
            with opener.open(base + exported["download_url"], timeout=30) as response:
                assert response.read(2) == b"PK"
            post("/api/app/quit", {})
            process.wait(timeout=20)
            assert process.returncode == 0
            print(f"便携包冒烟通过：{executable}")
            print(f"中文空格数据目录通过：{data}")
            print("离线主流程通过：建班、名单、周期、纸表 PDF、scorepack、安全退出")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
