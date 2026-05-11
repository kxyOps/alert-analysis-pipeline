"""pip install -e . 后提供的 kb / kb-server 命令：委托执行仓库 src/ 下的同名脚本。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _src_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def main_kb() -> None:
    script = _src_dir() / "kb.py"
    if not script.is_file():
        print(
            "找不到 kb.py：请在克隆后的仓库根目录执行 pip install -e .",
            file=sys.stderr,
        )
        sys.exit(1)
    raise SystemExit(subprocess.call([sys.executable, str(script)] + sys.argv[1:]))


def main_kb_server() -> None:
    script = _src_dir() / "kb-server.py"
    if not script.is_file():
        print(
            "找不到 kb-server.py：请在克隆后的仓库根目录执行 pip install -e .",
            file=sys.stderr,
        )
        sys.exit(1)
    raise SystemExit(subprocess.call([sys.executable, str(script)] + sys.argv[1:]))
