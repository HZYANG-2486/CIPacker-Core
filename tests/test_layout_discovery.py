"""布局探测与文件发现测试（P1）。

覆盖：CI 1.x / CI 2.x 结构识别、直接指向 ``data/`` 的情形、
程序目录与缓存目录的排除、备份开关。
"""

from __future__ import annotations

import unittest

from cipacker.discovery import iter_files
from cipacker.layout import LayoutKind, detect_layout

try:  # 支持 `python -m unittest discover tests`
    from .base import CIPackerTestCase
except ImportError:  # 支持 `cd tests && python -m unittest test_x`
    from base import CIPackerTestCase  # type: ignore


class TestLayoutDetection(CIPackerTestCase):
    def test_v1_detected(self):
        root = self.make_v1()
        layout = detect_layout(root)
        self.assertIs(layout.kind, LayoutKind.V1)
        self.assertEqual(layout.app_root, root)
        self.assertIsNone(layout.version_dir)
        self.assertTrue(layout.is_valid_install())

    def test_v2_detected_and_data_root_resolved(self):
        root = self.make_v2()
        layout = detect_layout(root)
        self.assertIs(layout.kind, LayoutKind.V2)
        # app_root 必须落到 data/ 而不是 app-<ver>-<n>/
        self.assertEqual(layout.app_root.name, "data")
        self.assertIsNotNone(layout.version_dir)
        self.assertTrue(str(layout.version_dir.name).startswith("app-"))
        self.assertTrue(layout.is_valid_install())

    def test_v2_when_pointing_directly_at_data(self):
        """用户直接把路径指到 data/ 也应正确识别。"""
        root = self.make_v2()
        layout = detect_layout(root / "data")
        self.assertIs(layout.kind, LayoutKind.V2)
        self.assertEqual(layout.app_root.name, "data")

    def test_unknown_for_arbitrary_dir(self):
        arbitrary = self.path("empty")
        arbitrary.mkdir()
        layout = detect_layout(arbitrary)
        self.assertIs(layout.kind, LayoutKind.UNKNOWN)

    def test_layout_paths(self):
        layout = detect_layout(self.make_v2())
        self.assertEqual(layout.settings_file.name, "Settings.json")
        self.assertEqual(layout.profiles_dir.name, "Profiles")
        self.assertEqual(layout.plugins_dir.name, "Plugins")
        self.assertEqual(layout.plugin_configs_dir.name, "Plugins")
        self.assertEqual(layout.plugin_configs_dir.parent.name, "Config")
        self.assertEqual(layout.automation_dir.name, "Automation")


class TestDiscovery(CIPackerTestCase):
    def test_excluded_dirs_are_pruned(self):
        root = self.make_v1()
        rels = {item.rel for item in iter_files(detect_layout(root))}
        for banned in ("Cache", "Logs", "Temp"):
            self.assertFalse(
                any(r.startswith(banned + "/") for r in rels),
                f"{banned}/ 不应被收集",
            )

    def test_plugins_index_excluded(self):
        root = self.make_v1()
        rels = {item.rel for item in iter_files(detect_layout(root))}
        self.assertNotIn("Config/PluginsIndex/index.json", rels)

    def test_program_binaries_excluded(self):
        root = self.make_v1()
        rels = {item.rel for item in iter_files(detect_layout(root))}
        self.assertNotIn("ClassIsland.exe", rels)
        self.assertNotIn("ClassIsland.dll", rels)

    def test_core_files_present(self):
        root = self.make_v1()
        rels = {item.rel for item in iter_files(detect_layout(root))}
        self.assertIn("Settings.json", rels)
        self.assertIn("Profiles/ClassPlans/cp-1.json", rels)
        self.assertIn("Config/Automation/Automations.json", rels)
        self.assertIn("Plugins/classisland.example.basic/manifest.yml", rels)

    def test_backups_opt_in(self):
        root = self.make_v1()
        layout = detect_layout(root)
        # 造一个备份
        (root / "Backups").mkdir(exist_ok=True)
        (root / "Backups" / "auto.zip.json").write_text("{}", encoding="utf-8")

        without = {i.rel for i in iter_files(layout, include_backups=False)}
        withb = {i.rel for i in iter_files(layout, include_backups=True)}
        self.assertFalse(any(r.startswith("Backups/") for r in without))
        self.assertTrue(any(r.startswith("Backups/") for r in withb))

    def test_results_are_sorted(self):
        root = self.make_v1()
        rels = [item.rel for item in iter_files(detect_layout(root))]
        self.assertEqual(rels, sorted(rels))


if __name__ == "__main__":
    unittest.main()
