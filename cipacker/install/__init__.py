"""运行环境相关的辅助（进程检测等）。"""

from .process import is_ci_running

__all__ = ["is_ci_running"]
