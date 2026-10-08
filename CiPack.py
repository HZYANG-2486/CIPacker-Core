#!/usr/bin/env python3
"""CIPacker 兼容入口（瘦 shim）。

自 v3.0 起，全部实现迁移到 :mod:`cipacker` 包中，本文件只保留一层薄壳，
以兼容旧有的调用方式：

* ``python CiPack.py pack ...``
* ``from CiPack import ...``（旧脚本可能引用的少量符号）
* ``python CiPack.py``（无子命令时打印帮助）

新代码请直接使用 :mod:`cipacker`：

.. code-block:: bash

    python -m cipacker pack D:\\ClassIsland -o backup.zip
    cipacker pack D:\\ClassIsland -o backup.zip      # 安装后

或作为库：

.. code-block:: python

    from cipacker import layout, pack, unpack, verify, doctor
"""

from __future__ import annotations

import sys

# --- 旧常量兼容（部分外部脚本/文档曾引用）--------------------------------- #
STRUCTURED_MODE = False

from cipacker import __version__  # noqa: E402
from cipacker.cli import main as _cli_main  # noqa: E402
from cipacker.errors import (  # noqa: E402
    EXIT_CANCELLED,
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_PRECONDITION,
    EXIT_USAGE,
)
from cipacker.structured import emit  # noqa: E402  兼容旧的 emit() 调用

__all__ = [
    "__version__",
    "main",
    "emit",
    "STRUCTURED_MODE",
    "EXIT_OK",
    "EXIT_FAILURE",
    "EXIT_USAGE",
    "EXIT_PRECONDITION",
    "EXIT_CANCELLED",
]


def main(argv: list[str] | None = None) -> int:
    """转发到 :func:`cipacker.cli.main`。

    与旧版行为保持一致：返回值即进程退出码；调用方负责 ``sys.exit``。
    """
    return _cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
