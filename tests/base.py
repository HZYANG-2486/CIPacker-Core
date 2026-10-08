"""共享测试基类与工具。"""

from __future__ import annotations

import atexit
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# 让 tests 包可以 import fixtures
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from fixtures import synth  # noqa: E402


class TempDirMixin:
    """为每个测试提供独立临时目录，测试结束自动清理。"""

    _tmp: Path

    def setUp(self) -> None:  # noqa: D102
        super().setUp()  # type: ignore[misc]
        self._tmp = Path(tempfile.mkdtemp(prefix="cipacker-test-"))
        atexit.register(shutil.rmtree, self._tmp, True)

    def tearDown(self) -> None:  # noqa: D102
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().tearDown()  # type: ignore[misc]

    # -- 便捷构造 ---------------------------------------------------------- #
    def path(self, name: str) -> Path:
        return self._tmp / name

    def make_v1(self, name: str = "v1") -> Path:
        return synth.make_v1_tree(self.path(name))

    def make_v2(self, name: str = "v2") -> Path:
        return synth.make_v2_tree(self.path(name))

    def make_sensitive(self, name: str = "sens") -> Path:
        return synth.make_sensitive_tree(self.path(name))

    def make_dirty(self, name: str = "dirty") -> Path:
        return synth.make_dirty_tree(self.path(name))


class CIPackerTestCase(TempDirMixin, unittest.TestCase):
    """推荐的测试基类。"""
