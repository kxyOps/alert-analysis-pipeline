#!/usr/bin/env python3
"""
开发用：轮询 kb-server.py 修改时间，变更后自动结束并重启子进程（stdlib only）。

用法:
  python3 scripts/kb-web-dev.py [端口]

环境变量:
  KB_PORT       端口（默认 8888，可被 argv 覆盖）
  KB_DATA_DIR   数据目录（默认 <项目>/data）

与本仓库「零依赖」一致：不引入 watchdog；适合本地改 UI/API 时省去手动重启。
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
KB_SERVER = ROOT / "src" / "kb-server.py"
POLL_INTERVAL = 0.6


def parse_port() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    return os.environ.get("KB_PORT", "8888")


def data_dir() -> str:
    return os.environ.get("KB_DATA_DIR", str(ROOT / "data"))


def main() -> None:
    if not KB_SERVER.is_file():
        print(f"[dev] 找不到 {KB_SERVER}", file=sys.stderr)
        sys.exit(1)

    port = parse_port()
    env = os.environ.copy()
    env["KB_DATA_DIR"] = data_dir()

    proc: Optional[subprocess.Popen] = None

    def kill_child() -> None:
        nonlocal proc
        if proc is None or proc.poll() is not None:
            proc = None
            return
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        proc = None

    def start_child() -> None:
        nonlocal proc
        proc = subprocess.Popen(
            [sys.executable, str(KB_SERVER), port],
            cwd=str(KB_SERVER.parent),
            env=env,
        )
        print(f"[dev] KB Web pid={proc.pid}  http://127.0.0.1:{port}/  （保存 kb-server.py 将自动重启）", flush=True)

    def on_sigint(_sig: int, _frame: object | None) -> None:
        kill_child()
        sys.exit(0)

    signal.signal(signal.SIGINT, on_sigint)

    mtime = KB_SERVER.stat().st_mtime
    start_child()

    try:
        while True:
            time.sleep(POLL_INTERVAL)
            try:
                cur = KB_SERVER.stat().st_mtime
            except OSError:
                continue

            if cur != mtime:
                print("[dev] 检测到 kb-server.py 变更，热重启…", flush=True)
                kill_child()
                mtime = cur
                start_child()
                continue

            if proc is not None and proc.poll() is not None:
                code = proc.returncode
                print(f"[dev] 子进程已退出 (exit {code})，1 秒后拉起…", flush=True)
                time.sleep(1)
                mtime = KB_SERVER.stat().st_mtime
                start_child()
    finally:
        kill_child()


if __name__ == "__main__":
    main()
