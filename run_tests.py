#!/usr/bin/env python3
"""运行 CIPacker 测试套件。

用法::

    python run_tests.py              # 运行全部
    python run_tests.py -v           # 详细输出
    python run_tests.py determinism  # 只跑文件名/类名匹配的用例
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "tests"


def main() -> int:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(TESTS))

    verbosity = 2 if "-v" in sys.argv else 1
    pattern = None
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            pattern = arg
            break

    loader = unittest.TestLoader()
    if pattern:
        # 先在 tests 目录内按文件名模糊匹配
        matches = [p for p in TESTS.glob("test_*.py") if pattern in p.stem]
        if matches:
            suite = unittest.TestSuite()
            for path in matches:
                module = f"tests.{path.stem}"
                suite.addTests(loader.loadTestsFromName(module))
        else:
            suite = loader.loadTestsFromName(pattern)
    else:
        suite = loader.discover(str(TESTS), pattern="test_*.py", top_level_dir=str(ROOT))

    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
