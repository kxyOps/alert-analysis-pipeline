"""KB 工具默认时区：可通过环境变量覆盖（开源场景避免写死 UTC+8）。"""
from __future__ import annotations

import os
from datetime import timedelta, timezone


def get_kb_timezone():
    """
    优先级：
    1) KB_TIMEZONE 或 TZ —— IANA 名称，如 Asia/Shanghai、UTC（需 Python zoneinfo 数据）
    2) KB_TZ_OFFSET —— 相对 UTC 的小时整数，如 8、-5
    3) 默认 UTC+8（兼容历史行为）
    """
    name = (os.environ.get('KB_TIMEZONE') or os.environ.get('TZ') or '').strip()
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            pass
    raw = (os.environ.get('KB_TZ_OFFSET') or '').strip()
    if raw:
        try:
            h = int(raw)
            if -12 <= h <= 14:
                return timezone(timedelta(hours=h))
        except ValueError:
            pass
    return timezone(timedelta(hours=8))
