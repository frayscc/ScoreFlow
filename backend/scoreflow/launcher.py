from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from .config import data_dir
from .main import SESSION_TOKEN, SHUTDOWN_REQUESTED, app, free_port


def _lock(file):
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.4) as response:
            return response.status == 200
    except Exception:
        return False


def _bootstrap_url(port: int, token: str) -> str:
    return f"http://127.0.0.1:{port}/session/bootstrap?token={token}"


def run() -> int:
    runtime_dir = data_dir() / ".runtime"
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = runtime_dir / "instance.lock"
    state_path = runtime_dir / "instance.json"
    lock_file = lock_path.open("a+b")
    try:
        _lock(lock_file)
    except OSError:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if _healthy(int(state["port"])):
                if os.environ.get("SCOREFLOW_NO_BROWSER") != "1":
                    webbrowser.open(_bootstrap_url(int(state["port"]), state["token"]))
                return 0
        except Exception:
            pass
        print("ScoreFlow 已有实例正在启动；请稍后重试。")
        return 1

    port = free_port()
    token = SESSION_TOKEN or secrets.token_urlsafe(32)
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid": os.getpid(), "port": port, "token": token}), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(state_path)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="scoreflow-server", daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if _healthy(port):
                if os.environ.get("SCOREFLOW_NO_BROWSER") != "1":
                    webbrowser.open(_bootstrap_url(port, token))
                break
            if not thread.is_alive():
                raise RuntimeError("本地服务启动失败")
            time.sleep(0.05)
        else:
            raise RuntimeError("本地服务健康检查超时")
        while thread.is_alive() and not SHUTDOWN_REQUESTED.wait(0.25):
            pass
        server.should_exit = True
        thread.join(timeout=10)
        return 0
    finally:
        state_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(run())
