"""标准流编码健壮性测试（Windows 兼容性回归）。

**背景**

简体中文 Windows 上，控制台/管道默认使用 GBK(cp936)。当输出被重定向到
文件或管道时（CI、``> out.txt``、``| more``、GUI 捕获子进程输出），
Python 会按 GBK 编码 stdout/stderr，此时体检报告里的 ``✓``(U+2713) /
``✗``(U+2717) 无法编码，``print`` 抛 ``UnicodeEncodeError``，
导致 ``doctor`` / ``pack --doctor`` / ``pack --json-report`` 整个命令崩溃。

这些用例在 Linux 上通过 ``PYTHONIOENCODING=gbk`` 复现同一场景，
因此无需 Windows 即可守住这条回归线。
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
from pathlib import Path

from cipacker import commands
from cipacker.cli import _configure_stdio

try:
    from .base import CIPackerTestCase
except ImportError:
    from base import CIPackerTestCase  # type: ignore

ROOT = Path(__file__).resolve().parent.parent


class TestSeverityMarkFallback(unittest.TestCase):
    """符号标记应按输出流编码能力自适应。"""

    def _marks_for(self, encoding: str) -> dict[str, str]:
        original = sys.stdout
        try:
            sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding=encoding)
            return commands._severity_marks()
        finally:
            sys.stdout = original

    def test_gbk_falls_back_to_ascii(self):
        marks = self._marks_for("gbk")
        self.assertEqual(marks, commands._ASCII_MARKS)
        # 兜底标记必须全部可被 GBK 编码，否则仍会崩溃
        for value in marks.values():
            value.encode("gbk")

    def test_utf8_uses_symbols(self):
        self.assertEqual(self._marks_for("utf-8"), commands._FANCY_MARKS)

    def test_ascii_encoding_falls_back(self):
        """更严格的编码（纯 ASCII）同样应降级。"""
        self.assertEqual(self._marks_for("ascii"), commands._ASCII_MARKS)


class TestConfigureStdio(unittest.TestCase):
    """``_configure_stdio`` 应把标准流切到 UTF-8 且启用替换式错误处理。"""

    def test_sets_utf8_and_replace(self):
        original_out, original_err = sys.stdout, sys.stderr
        try:
            sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
            sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
            _configure_stdio()
            self.assertEqual((sys.stdout.encoding or "").lower().replace("-", ""), "utf8")
            self.assertEqual((sys.stderr.encoding or "").lower().replace("-", ""), "utf8")
            self.assertEqual(sys.stdout.errors, "replace")
            self.assertEqual(sys.stderr.errors, "replace")
            # 关键：配置后写入 ✓✗ 不再抛异常
            sys.stdout.write("✓✗\n")
            sys.stdout.flush()
        finally:
            sys.stdout, sys.stderr = original_out, original_err

    def test_tolerates_unreconfigurable_stream(self):
        """被替换成不可重配置的对象时不应抛异常。"""

        class Opaque:
            def write(self, _data):  # pragma: no cover - 仅为满足接口
                pass

        original = sys.stdout
        try:
            sys.stdout = Opaque()  # type: ignore[assignment]
            _configure_stdio()  # 不应抛异常
        finally:
            sys.stdout = original


class TestCliSurvivesGbk(CIPackerTestCase):
    """端到端：在 GBK 环境下运行各命令，进程不得因编码崩溃。"""

    def _run_gbk(self, *argv: str) -> tuple[int, str]:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "gbk"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "CiPack.py"), *argv],
            capture_output=True,
            env=env,
            cwd=str(ROOT),
        )
        combined = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
        return proc.returncode, combined

    def test_doctor_does_not_crash(self):
        src = self.make_v1()
        code, output = self._run_gbk("doctor", str(src))
        self.assertNotIn("codec can't encode", output)
        self.assertNotIn("Traceback", output)
        # 纯报告模式：即使发现问题也返回 0
        self.assertEqual(code, 0, output)

    def test_pack_with_doctor_does_not_crash(self):
        src = self.make_v1()
        code, output = self._run_gbk(
            "pack", str(src), "-o", str(self.path("a.zip")), "--doctor"
        )
        self.assertNotIn("codec can't encode", output)
        self.assertNotIn("Traceback", output)
        # 夹具含缺失依赖，体检应报 error 并中止（rc=1），而不是编码崩溃
        self.assertEqual(code, 1, output)

    def test_pack_json_report_does_not_crash(self):
        src = self.make_v1()
        code, output = self._run_gbk(
            "pack",
            str(src),
            "-o",
            str(self.path("b.zip")),
            "--json-report",
            str(self.path("r.json")),
        )
        self.assertNotIn("codec can't encode", output)
        self.assertNotIn("Traceback", output)
        self.assertEqual(code, 0, output)

    def test_report_content_still_readable(self):
        """修复后报告内容应完整（而不是被替换成问号）。"""
        src = self.make_v1()
        _code, output = self._run_gbk("doctor", str(src), "--json")
        self.assertIn("findings", output)
