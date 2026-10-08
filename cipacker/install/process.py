"""检测 ClassIsland 是否正在运行。

解包/覆盖配置前必须确认 CI 已退出，否则写入可能失败或导致配置损坏。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: 进程名（不含扩展名，大小写不敏感）。
PROCESS_NAMES = ("ClassIsland.exe", "ClassIsland")


def is_ci_running() -> bool:
    """检测 ClassIsland 进程是否存在。"""
    if sys.platform == "win32":
        return _check_windows()
    return _check_posix()


def _check_windows() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ClassIsland.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "ClassIsland.exe" in (result.stdout or "")


def _check_posix() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "ClassIsland"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def describe_lock_hint(target: Path | None = None) -> str:
    """生成「请先关闭 ClassIsland」的提示文案。"""
    where = f"（{target}）" if target else ""
    return f"检测到 ClassIsland 正在运行{where}，请先完全退出后再试"
