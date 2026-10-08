"""``--structured`` GUI 事件流契约测试（关键）。

GUI 外壳依赖的硬性约定：

1. 所有结构化事件以 ``[STRUCTURED] `` 前缀写入 **stderr**
2. 每次命令调用 **有且仅有** 一条 ``result=`` 事件
3. ``result=`` 携带 ``exit_code``；失败时携带 ``message``
4. 人类可读输出仍在 stdout，不污染事件流

这些约定一旦破坏，GUI 外壳就会解析失败，因此逐条断言。
"""

from __future__ import annotations

import io
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout

from cipacker import structured
from cipacker.cli import main

try:
    from .base import CIPackerTestCase
except ImportError:
    from base import CIPackerTestCase  # type: ignore

from fixtures.synth import make_v1_tree
from test_pack_determinism import _pack

PREFIX = "[STRUCTURED] "


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """执行 CLI，返回 ``(exit_code, stdout, stderr)``。"""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def events(stderr: str) -> list[str]:
    return [ln for ln in stderr.splitlines() if ln.startswith(PREFIX)]


class TestStructuredContract(CIPackerTestCase):
    def setUp(self):
        super().setUp()
        self.src = make_v1_tree(self.path("src"))
        self.pkg = self.path("pkg.zip")
        _pack(self.src, self.pkg)

    # -- 断言工具 ---------------------------------------------------------- #
    def assert_exactly_one_result(self, stderr: str) -> str:
        results = [e for e in events(stderr) if " result=" in e]
        self.assertEqual(
            len(results),
            1,
            f"应恰好一条 result= 事件，实际 {len(results)} 条:\n{stderr}",
        )
        return results[0]

    # -- 各命令 ------------------------------------------------------------ #
    def test_pack_success(self):
        code, out, err = run_cli(
            ["pack", str(self.src), "-o", str(self.path("o.zip")), "--structured"]
        )
        self.assertEqual(code, 0)
        line = self.assert_exactly_one_result(err)
        self.assertIn("result=success", line)
        self.assertIn("exit_code=0", line)
        # 人类输出仍在 stdout
        self.assertIn("打包完成", out)
        self.assertNotIn(PREFIX, out)

    def test_pack_failure(self):
        code, _out, err = run_cli(
            ["pack", str(self.path("nope")), "--structured"]
        )
        self.assertEqual(code, 1)
        line = self.assert_exactly_one_result(err)
        self.assertIn("result=failed", line)
        self.assertIn("exit_code=1", line)
        self.assertIn("message=", line)

    def test_list_success(self):
        code, _out, err = run_cli(["list", str(self.pkg), "--structured"])
        self.assertEqual(code, 0)
        self.assert_exactly_one_result(err)

    def test_verify_success(self):
        code, _out, err = run_cli(["verify", str(self.pkg), "--structured"])
        self.assertEqual(code, 0)
        line = self.assert_exactly_one_result(err)
        self.assertIn("result=success", line)

    def test_verify_failure(self):
        code, _out, err = run_cli(
            ["verify", str(self.path("missing.zip")), "--structured"]
        )
        self.assertEqual(code, 1)
        line = self.assert_exactly_one_result(err)
        self.assertIn("result=failed", line)

    def test_doctor_report_mode_exits_zero(self):
        code, _out, err = run_cli(["doctor", str(self.src), "--structured"])
        self.assertEqual(code, 0)
        self.assert_exactly_one_result(err)

    def test_doctor_strict_failure(self):
        code, _out, err = run_cli(
            ["doctor", str(self.src), "--strict", "--structured"]
        )
        self.assertEqual(code, 1)
        line = self.assert_exactly_one_result(err)
        self.assertIn("result=failed", line)

    def test_unpack_dry_run_no_prompt(self):
        dest = self.path("dest")
        dest.mkdir()
        code, _out, err = run_cli(
            [
                "unpack",
                str(self.pkg),
                "-d",
                str(dest),
                "--force",
                "--dry-run",
                "--structured",
            ]
        )
        self.assertEqual(code, 0)
        self.assert_exactly_one_result(err)
        self.assertEqual(list(dest.iterdir()), [], "dry-run 不得写入任何文件")

    def test_unpack_precondition_exit_code(self):
        """缺少 CI 安装且未强制 -> 退出码 3。"""
        dest = self.path("dest2")
        dest.mkdir()
        code, _out, err = run_cli(
            ["unpack", str(self.pkg), "-d", str(dest), "--structured"]
        )
        self.assertEqual(code, 3)
        line = self.assert_exactly_one_result(err)
        self.assertIn("exit_code=3", line)

    def test_unpack_real(self):
        dest = self.path("dest3")
        dest.mkdir()
        code, _out, err = run_cli(
            ["unpack", str(self.pkg), "-d", str(dest), "--force", "-y", "--structured"]
        )
        self.assertEqual(code, 0)
        self.assert_exactly_one_result(err)
        self.assertTrue((dest / "Settings.json").exists())

    def test_stage_events_present(self):
        _code, _out, err = run_cli(
            ["pack", str(self.src), "-o", str(self.path("s.zip")), "--structured"]
        )
        stages = re.findall(r"stage=(\w+)", err)
        self.assertIn("pack", stages)

    def test_finding_events_for_doctor(self):
        _code, _out, err = run_cli(
            ["doctor", str(self.src), "--structured"]
        )
        # 该 fixture 含缺失依赖 -> 至少一条 finding
        self.assertIn("level=finding", err)

    def test_no_structured_output_without_flag(self):
        _code, out, err = run_cli(["pack", str(self.src), "-o", str(self.path("n.zip"))])
        self.assertEqual(events(err), [], "未加 --structured 时不应输出事件")
        self.assertTrue(out.strip())


class TestStructuredModule(unittest.TestCase):
    """直接单测 structured 模块的序列化。"""

    def setUp(self):
        self.buf = io.StringIO()
        self._orig = structured.STRUCTURED_MODE
        structured.set_structured(True)

    def tearDown(self):
        structured.set_structured(self._orig)

    def _capture(self, func) -> str:
        err = io.StringIO()
        with redirect_stderr(err):
            func()
        return err.getvalue()

    def test_emit_only_when_structured(self):
        structured.set_structured(False)
        err = self._capture(lambda: structured.stage("x"))
        self.assertEqual(err, "")

    def test_emit_format(self):
        err = self._capture(lambda: structured.stage("pack", file_count=18))
        self.assertTrue(err.startswith(PREFIX))
        self.assertIn("stage=pack", err)
        self.assertIn("file_count=18", err)

    def test_values_with_spaces_are_quoted(self):
        err = self._capture(lambda: structured.error("a b c"))
        self.assertIn('"a b c"', err)

    def test_parse_line_roundtrip(self):
        err = self._capture(lambda: structured.stage("verify", total=3))
        line = err.strip()
        parsed = structured.parse_line(line)
        self.assertEqual(parsed.get("stage"), "verify")
        self.assertEqual(parsed.get("total"), "3")

    def test_progress_event(self):
        err = self._capture(lambda: structured.progress(50.0, 5, 10))
        self.assertIn("progress=50.0", err)
        self.assertIn("current=5", err)
        self.assertIn("total=10", err)

    def test_quoted_roundtrip_via_parse_line(self):
        """含空格/中文的消息必须能被 parse_line 无损还原。"""
        msg = "体检未通过（健康分 0）"
        err = self._capture(lambda: structured.error(msg))
        parsed = structured.parse_line(err.strip())
        self.assertEqual(parsed["message"], msg)

    def test_quote_escaping_roundtrip(self):
        msg = 'he said "hi" \\ bye'
        err = self._capture(lambda: structured.warning(msg))
        parsed = structured.parse_line(err.strip())
        self.assertEqual(parsed["message"], msg)

    def test_result_event(self):
        err = self._capture(lambda: structured.result("failed", 1, "boom"))
        self.assertIn("result=failed", err)
        self.assertIn("exit_code=1", err)
        self.assertIn("message=boom", err)


if __name__ == "__main__":
    unittest.main()
