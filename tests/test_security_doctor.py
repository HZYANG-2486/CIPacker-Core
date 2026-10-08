"""安全、校验与体检测试（P4 / P5）。

覆盖：

* Zip Slip 防护（``..``、绝对路径、盘符、NUL、符号链接式路径）
* 篡改检测（内容哈希 + 指纹）
* 备份-回滚：解包前备份被覆盖文件
* doctor 的 7 类规则与精准脱敏
"""

from __future__ import annotations

import json
import unittest
import zipfile
from pathlib import Path

from cipacker.archive import is_safe_member
from cipacker.doctor import (
    DoctorOptions,
    redact_file,
    run,
    run_on_archive,
)
from cipacker.layout import detect_layout
from cipacker.unpack import build_plan, execute_plan
from cipacker.verify import verify_archive

try:
    from .base import CIPackerTestCase
except ImportError:
    from base import CIPackerTestCase  # type: ignore

from test_pack_determinism import _pack


class TestZipSlip(unittest.TestCase):
    def test_rejects_parent_traversal(self):
        root = Path("/tmp/target")
        self.assertFalse(is_safe_member("../../etc/passwd", root)[0])
        self.assertFalse(is_safe_member("a/../../../etc/passwd", root)[0])

    def test_rejects_absolute_paths(self):
        root = Path("/tmp/target")
        self.assertFalse(is_safe_member("/etc/passwd", root)[0])
        self.assertFalse(is_safe_member("C:/Windows/system32", root)[0])

    def test_rejects_null_byte(self):
        self.assertFalse(is_safe_member("a\x00b", Path("/tmp/target"))[0])

    def test_accepts_normal_paths(self):
        for good in (
            "Settings.json",
            "Profiles/x.json",
            "Plugins/p/manifest.yml",
            "a/b/c/d.bin",
        ):
            ok, reason = is_safe_member(good, Path("/tmp/target"))
            self.assertTrue(ok, f"{good} 应被接受（{reason}）")


class TestVerify(CIPackerTestCase):
    def test_clean_archive_passes(self):
        root = self.make_v1()
        out = self.path("ok.zip")
        _pack(root, out)
        result = verify_archive(out)
        self.assertTrue(result.ok, [i.message for i in result.issues])

    def test_modified_member_detected(self):
        root = self.make_v1()
        out = self.path("ok.zip")
        _pack(root, out)

        tampered = self.path("bad.zip")
        with zipfile.ZipFile(out) as zin, zipfile.ZipFile(
            tampered, "w", zipfile.ZIP_DEFLATED
        ) as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename == "Settings.json":
                    data = data.replace(b"Light", b"DARK!!")
                zout.writestr(info, data)

        result = verify_archive(tampered)
        self.assertFalse(result.ok)
        kinds = {i.kind for i in result.issues}
        self.assertIn("modified", kinds)
        self.assertIn("fingerprint", kinds)

    def test_deep_detects_archive_hash_mismatch(self):
        """--deep 额外比对归档整体哈希（sidecar 记录的值）。"""
        root = self.make_v1()
        out = self.path("deep.zip")
        _pack(root, out)

        # 篡改 sidecar 中记录的归档哈希
        sidecar = out.with_suffix(out.suffix + ".sha256")
        self.assertTrue(sidecar.exists())
        sidecar.write_text(
            "0" * 64 + "  " + out.name + "\n",
            encoding="utf-8",
        )

        shallow = verify_archive(out, deep=False)
        deep = verify_archive(out, deep=True)
        self.assertTrue(shallow.ok, "浅校验只比对内容，应通过")
        self.assertFalse(deep.ok, "深校验应发现归档哈希不匹配")
        self.assertIn("archive", {i.kind for i in deep.issues})

    def test_extra_member_detected(self):
        root = self.make_v1()
        out = self.path("ok.zip")
        _pack(root, out)

        zout_path = self.path("extra.zip")
        with zipfile.ZipFile(out) as zin, zipfile.ZipFile(
            zout_path, "w", zipfile.ZIP_DEFLATED
        ) as zout:
            for info in zin.infolist():
                zout.writestr(info, zin.read(info.filename))
            zout.writestr("Sneaky/new.json", b"{}")

        result = verify_archive(zout_path)
        self.assertFalse(result.ok)
        self.assertIn("added", {i.kind for i in result.issues})

    def test_missing_member_detected(self):
        root = self.make_v1()
        out = self.path("ok.zip")
        _pack(root, out)

        zout_path = self.path("missing.zip")
        with zipfile.ZipFile(out) as zin, zipfile.ZipFile(
            zout_path, "w", zipfile.ZIP_DEFLATED
        ) as zout:
            for info in zin.infolist():
                if info.filename == "Settings.json":
                    continue
                zout.writestr(info, zin.read(info.filename))

        result = verify_archive(zout_path)
        self.assertFalse(result.ok)
        self.assertIn("missing", {i.kind for i in result.issues})


class TestBackupOnUnpack(CIPackerTestCase):
    def test_existing_files_are_backed_up(self):
        """解包覆盖已有配置前应生成备份，使操作可回滚。"""
        src = self.make_v1("src")
        out = self.path("pkg.zip")
        _, _, _, _, _, _, _ = _pack(src, out)

        target = self.path("target")
        target.mkdir()
        (target / "Settings.json").write_text('{"original": true}', encoding="utf-8")

        layout = detect_layout(target)
        result = execute_plan(out, layout, build_plan(out, layout), make_backup=True)

        self.assertIsNotNone(result.backup_dir, "应产生备份目录")
        self.assertTrue(result.backup_dir.is_dir())
        # 备份目录内应含被覆盖文件的副本
        backed = {f.relative_to(result.backup_dir).as_posix()
                  for f in result.backup_dir.rglob("*") if f.is_file()}
        self.assertIn("Settings.json", backed, f"备份内容: {backed}")

    def test_no_backup_when_disabled(self):
        src = self.make_v1("src")
        out = self.path("pkg.zip")
        _pack(src, out)

        target = self.path("target")
        target.mkdir()
        layout = detect_layout(target)
        result = execute_plan(out, layout, build_plan(out, layout), make_backup=False)
        self.assertIsNone(result.backup_dir)


class TestDoctorRules(CIPackerTestCase):
    def test_invalid_json_reported(self):
        root = self.make_dirty()
        layout = detect_layout(root)
        report = run(layout)
        self.assertTrue(any(f.rule_id == "broken-json" for f in report.findings), [f.rule_id for f in report.findings])
        self.assertGreater(report.counts.get("error", 0), 0)

    def test_orphan_plugin_config(self):
        root = self.make_v1()
        report = run(detect_layout(root))
        self.assertTrue(
            any("ghost" in (f.location or "") for f in report.findings),
            "应检出无对应插件的孤儿配置",
        )

    def test_disabled_plugin_informational(self):
        root = self.make_v1()
        report = run(detect_layout(root))
        infos = [f for f in report.findings if f.severity.value == "info"]
        self.assertTrue(any("禁用" in f.message for f in infos))

    def test_missing_dependency_reported(self):
        root = self.make_v1()
        report = run(detect_layout(root))
        self.assertTrue(
            any(f.severity.value == "error" and "依赖" in f.message for f in report.findings)
        )

    def test_credentials_detected(self):
        root = self.make_sensitive()
        report = run(detect_layout(root))
        cred = [f for f in report.findings if f.rule_id == "credential-exposure"]
        self.assertTrue(cred, "应检出敏感信息")
        # 报告内不得回显明文口令
        blob = json.dumps(report.to_dict(), ensure_ascii=False)
        self.assertNotIn("Sup3rSecret!", blob)
        self.assertNotIn("ghp_0123456789", blob)

    def test_absolute_paths_detected(self):
        root = self.make_sensitive()
        report = run(detect_layout(root))
        self.assertTrue(any(f.rule_id == "abs-path-leak" for f in report.findings))

    def test_strict_mode_escalates(self):
        root = self.make_sensitive()
        normal = run(detect_layout(root), options=DoctorOptions(strict=False))
        strict = run(detect_layout(root), options=DoctorOptions(strict=True))
        self.assertGreaterEqual(
            strict.score if hasattr(strict, "score") else 0,
            0,
        )
        self.assertLessEqual(strict.score, normal.score)

    def test_options_can_skip_checks(self):
        root = self.make_sensitive()
        report = run(
            detect_layout(root),
            options=DoctorOptions(check_credentials=False, check_paths=False),
        )
        self.assertNotIn("credential-exposure", [f.rule_id for f in report.findings])


class TestRedaction(CIPackerTestCase):
    def test_redact_file_masks_secrets_preserving_structure(self):
        root = self.make_sensitive()
        target = (
            detect_layout(root).plugin_configs_dir
            / "classisland.example.basic"
            / "settings.json"
        )
        before = json.loads(target.read_text(encoding="utf-8"))
        changed = redact_file(target)
        after = json.loads(target.read_text(encoding="utf-8"))

        self.assertTrue(changed, "应报告有字段被脱敏")
        # 结构（键集合）必须保持不变
        self.assertEqual(set(before.keys()), set(after.keys()))
        # 非敏感字段原样保留
        self.assertEqual(before["ServerHost"], after["ServerHost"])
        # 敏感字段被掩码
        self.assertNotEqual(before["Password"], after["Password"])
        self.assertIn("*", after["Password"])

    def test_redact_preserves_non_string_types(self):
        root = self.make_sensitive()
        target = detect_layout(root).settings_file
        before = json.loads(target.read_text(encoding="utf-8"))
        redact_file(target)
        after = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(before["IsAutoUpdateEnabled"], after["IsAutoUpdateEnabled"])

    def test_redact_archive_via_doctor(self):
        root = self.make_sensitive()
        out = self.path("sens.zip")
        _pack(root, out)
        report = run_on_archive(out)
        self.assertTrue(report.findings)


if __name__ == "__main__":
    unittest.main()


class TestRedactDoesNotTouchSource(CIPackerTestCase):
    """回归：`pack --redact` 绝不允许改写用户的真实配置。

    历史缺陷：`_redact_items` 直接对 ``item.source``（即源目录中的真实文件）
    调用就地脱敏，导致用户口令被永久破坏。
    """

    def setUp(self):
        super().setUp()
        self.src = self.make_sensitive()
        from cipacker.commands import pack_command

        self.pack_command = pack_command

    def _settings(self):
        return (
            detect_layout(self.src).plugin_configs_dir
            / "classisland.example.basic"
            / "settings.json"
        )

    def test_source_files_unchanged(self):
        import json

        from cipacker.cli import main

        before = json.loads(self._settings().read_text(encoding="utf-8"))
        out = self.path("r.zip")

        code = main(["pack", str(self.src), "-o", str(out), "--redact"])
        self.assertEqual(code, 0)

        after = json.loads(self._settings().read_text(encoding="utf-8"))
        self.assertEqual(before, after, "源目录文件被就地改写了！")

    def test_package_content_is_redacted(self):
        import json
        import zipfile

        from cipacker.cli import main

        out = self.path("r2.zip")
        main(["pack", str(self.src), "-o", str(out), "--redact"])

        with zipfile.ZipFile(out) as zf:
            data = json.loads(
                zf.read("Config/Plugins/classisland.example.basic/settings.json")
            )

        self.assertEqual(data["Password"], "***", "包内应已脱敏")
        self.assertEqual(data["ServerHost"], "https://example.invalid", "非敏感字段须保留")

    def test_redact_temp_dir_cleaned(self):
        import glob
        import tempfile

        from cipacker.cli import main

        tmp_root = tempfile.gettempdir()
        before = set(glob.glob(f"{tmp_root}/cipacker-redact-*"))
        main(["pack", str(self.src), "-o", str(self.path("r3.zip")), "--redact"])
        after = set(glob.glob(f"{tmp_root}/cipacker-redact-*"))
        self.assertEqual(before, after, "脱敏临时目录未被清理")


class TestSymlinkSafety(CIPackerTestCase):
    """回归：符号链接不得被跟随。

    历史缺陷：数据目录内的符号链接会被 ``open()`` 跟随，
    从而把宿主机任意文件（如 /etc/shadow）的内容打进迁移包。
    """

    def test_symlinked_file_excluded_from_package(self):
        import os

        from cipacker.discovery import iter_files
        from cipacker.layout import detect_layout

        root = self.make_v1()
        outside = self.path("secret.txt")
        outside.write_text("TOP_SECRET", encoding="utf-8")
        os.symlink(outside, root / "Profiles" / "leak.json")

        rels = {i.rel for i in iter_files(detect_layout(root))}
        self.assertNotIn("Profiles/leak.json", rels, "符号链接不应被收集")

    def test_symlink_content_not_in_archive(self):
        import os
        import zipfile

        from cipacker.cli import main

        root = self.make_v1()
        outside = self.path("secret2.txt")
        outside.write_text("TOP_SECRET_2", encoding="utf-8")
        os.symlink(outside, root / "Profiles" / "leak2.json")

        out = self.path("s.zip")
        main(["pack", str(root), "-o", str(out)])

        with zipfile.ZipFile(out) as zf:
            blob = b"".join(zf.read(n) for n in zf.namelist())
        self.assertNotIn(b"TOP_SECRET_2", blob, "符号链接目标内容泄漏进了包")


class TestDuplicateMembers(CIPackerTestCase):
    """回归：包内重复路径不得静默覆盖。"""

    def test_duplicate_member_skipped(self):
        import zipfile

        from cipacker.layout import detect_layout
        from cipacker.unpack import build_plan

        dup = self.path("dup.zip")
        with zipfile.ZipFile(dup, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Settings.json", '{"a": 1}')
            zf.writestr("Settings.json", '{"b": 2}')  # 同名覆盖

        target = self.path("t")
        target.mkdir()
        plan = build_plan(dup, detect_layout(target))

        self.assertEqual(len(plan.items), 1, "重复条目应只保留一条")
        self.assertTrue(plan.skipped, "重复条目应记入 skipped")
        self.assertTrue(
            any("重复" in reason for _name, reason in plan.skipped),
            f"跳过原因应说明重复: {plan.skipped}",
        )


class TestAtomicWriteCleanup(CIPackerTestCase):
    """回归：原子写入失败必须清理临时文件。"""

    def test_no_ciptmp_left_behind(self):
        from cipacker.cli import main

        src = self.make_v1()
        out = self.path("a.zip")
        main(["pack", str(src), "-o", str(out)])

        dest = self.path("dest")
        dest.mkdir()
        main(["unpack", str(out), "-d", str(dest), "--force", "-y"])

        leftovers = list(dest.rglob("*.ciptmp"))
        self.assertEqual(leftovers, [], f"残留临时文件: {leftovers}")


class TestPackDoctorGate(CIPackerTestCase):
    """回归：``pack`` 的体检门禁语义。

    历史缺陷：``--json-report`` 与 ``--doctor`` 共用同一分支，导致只想
    「顺带导出报告」的调用被体检错误整体中止；同时 ``--doctor`` 的阻断条件
    未与 ``doctor`` 子命令的语义对齐（默认纯报告不阻断）。

    align 后的约定：

    * ``--json-report`` 单独出现 → 永不阻断，报告照常写出
    * ``--doctor``               → 仅 ``error`` 阻断
    * ``--doctor --strict``      → ``error`` 与 ``warning`` 都阻断
    """

    def _healthy(self, name: str = "healthy") -> Path:
        """构造一份体检无错的 v1 夹具。"""
        root = self.make_v1(name)
        import shutil as _sh

        _sh.rmtree(root / "Plugins" / "classisland.example.orphan-dep", True)
        _sh.rmtree(root / "Config" / "Plugins" / "classisland.example.ghost", True)
        return root

    def test_json_report_alone_never_blocks(self):
        """即使夹具含硬错误，只给 --json-report 也必须正常出包。"""
        from cipacker.cli import main

        src = self.make_v1()  # 含 orphan-dep：体检有 error
        out = self.path("p.zip")
        rep = self.path("rep.json")

        code = main(["pack", str(src), "-o", str(out), "--json-report", str(rep)])

        self.assertEqual(code, 0, "--json-report 不应阻断打包")
        self.assertTrue(out.is_file(), "包应已生成")
        self.assertTrue(rep.is_file(), "报告应已写出")

    def test_doctor_blocks_on_error(self):
        from cipacker.cli import main

        src = self.make_v1()
        code = main(
            ["pack", str(src), "-o", str(self.path("p.zip")), "--doctor"]
        )
        self.assertEqual(code, 1, "--doctor 遇 error 应中止")

    def test_doctor_passes_when_healthy(self):
        from cipacker.cli import main

        src = self._healthy()
        out = self.path("p.zip")
        code = main(["pack", str(src), "-o", str(out), "--doctor"])
        self.assertEqual(code, 0, "健康夹具下 --doctor 应通过")
        self.assertTrue(out.is_file())

    def test_strict_blocks_on_warning_only(self):
        """--strict 把仅含警告的报告升级为阻断。"""
        from cipacker.cli import main

        src = self._healthy()
        # 人为注入一条 warning 且不含 error：孤立插件配置
        ghost = src / "Config" / "Plugins" / "classisland.example.ghost2"
        ghost.mkdir(parents=True, exist_ok=True)
        (ghost / "settings.json").write_text("{}", encoding="utf-8")

        relaxed = main(
            ["pack", str(src), "-o", str(self.path("r.zip")), "--doctor"]
        )
        self.assertEqual(relaxed, 0, "非 strict 下仅警告不应阻断")

        strict = main(
            [
                "pack",
                str(src),
                "-o",
                str(self.path("s.zip")),
                "--doctor",
                "--strict",
            ]
        )
        self.assertEqual(strict, 1, "--strict 下警告应阻断")
