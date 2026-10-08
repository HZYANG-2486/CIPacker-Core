"""异常族与退出码映射。

统一约定：

===== ==================================================
退出码 含义
===== ==================================================
0     成功
1     一般性失败（I/O、校验失败、结构不兼容等）
2     命令行用法错误（由 argparse 产生）
3     前置条件不满足（CI 未安装、用户取消等）
130   用户中断（Ctrl+C）
===== ==================================================
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_PRECONDITION = 3
EXIT_CANCELLED = 130


class CIPackerError(Exception):
    """所有 CIPacker 异常的基类。"""

    exit_code = EXIT_FAILURE

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class UsageError(CIPackerError):
    """命令行参数无效。"""

    exit_code = EXIT_USAGE


class PreconditionError(CIPackerError):
    """运行前置条件不满足（例如目标目录不是有效 CI 安装）。"""

    exit_code = EXIT_PRECONDITION


class CancelledError(CIPackerError):
    """用户主动取消操作。"""

    exit_code = EXIT_CANCELLED


class LayoutError(CIPackerError):
    """无法识别或结构不兼容。"""


class PackError(CIPackerError):
    """打包失败。"""


class UnpackError(CIPackerError):
    """解包失败。"""


class VerifyError(CIPackerError):
    """校验失败（包损坏或被篡改）。"""


class DownloadError(CIPackerError):
    """下载失败。"""


class DoctorError(CIPackerError):
    """体检失败。"""
