"""YAML 子集解析与插件依赖闭包测试（P2）。

重点回归：原 ``parse_yaml_simple`` 无法解析的列表/嵌套结构
（``dependencies``、``supportedOSPlatforms``）必须被正确解析。
"""

from __future__ import annotations

import unittest

from cipacker.layout import detect_layout
from cipacker.plugins import (
    build_dependency_index,
    check_api_compatibility,
    check_dependencies,
    load_plugin_manifest,
    parse_dependencies,
    parse_supported_os,
    scan_plugins,
)
from cipacker.yamlish import load, load_mapping

try:
    from .base import CIPackerTestCase
except ImportError:
    from base import CIPackerTestCase  # type: ignore

from fixtures.synth import make_plugin

MANIFEST_WITH_LISTS = """\
id: classisland.example.rich
name: 富清单插件
description: 含列表与嵌套
version: 1.2.3
apiVersion: 2.0.0.0
author: synth
entranceAssembly: Plugin.dll
url: https://example.invalid/p
dependencies:
  - id: classisland.example.base
    version: 1.0.0
  - id: classisland.example.optional
supportedOSPlatforms:
  - Windows
  - Linux
"""


class TestYamlSubset(CIPackerTestCase):
    def test_scalars(self):
        data = load_mapping("a: 1\nb: hello\nc: true\nd: null\ne: 1.5")
        self.assertEqual(data["a"], 1)
        self.assertEqual(data["b"], "hello")
        self.assertIs(data["c"], True)
        self.assertIsNone(data["d"])
        self.assertEqual(data["e"], 1.5)

    def test_quoted_and_special(self):
        data = load_mapping('a: "he: llo"\nb: \'x#y\'\nc: value  # comment')
        self.assertEqual(data["a"], "he: llo")
        self.assertEqual(data["b"], "x#y")
        self.assertEqual(data["c"], "value")

    def test_block_list_of_scalars(self):
        data = load_mapping("items:\n  - one\n  - two\n  - three")
        self.assertEqual(data["items"], ["one", "two", "three"])

    def test_list_of_mappings(self):
        """原 parse_yaml_simple 完全无法处理的情形。"""
        data = load_mapping(MANIFEST_WITH_LISTS)
        deps = data["dependencies"]
        self.assertIsInstance(deps, list)
        self.assertEqual(len(deps), 2)
        self.assertEqual(deps[0]["id"], "classisland.example.base")
        self.assertEqual(deps[0]["version"], "1.0.0")
        self.assertEqual(deps[1]["id"], "classisland.example.optional")
        self.assertNotIn("version", deps[1])

    def test_nested_supported_os(self):
        data = load_mapping(MANIFEST_WITH_LISTS)
        self.assertEqual(data["supportedOSPlatforms"], ["Windows", "Linux"])

    def test_block_scalar(self):
        data = load_mapping("readme: |\n  第一行\n  第二行\n")
        self.assertIn("第一行", data["readme"])
        self.assertIn("第二行", data["readme"])

    def test_empty_and_comment_only(self):
        self.assertEqual(load_mapping(""), {})
        self.assertEqual(load_mapping("# only a comment\n"), {})

    def test_deep_nesting(self):
        data = load_mapping("a:\n  b:\n    c:\n      d: deep")
        self.assertEqual(data["a"]["b"]["c"]["d"], "deep")

    def test_load_returns_scalar(self):
        self.assertEqual(load("42"), 42)
        self.assertEqual(load("just a string"), "just a string")


class TestPluginParsing(CIPackerTestCase):
    def test_parse_dependencies_mapping_form(self):
        deps = parse_dependencies([{"id": "a"}, {"id": "b", "isRequired": False}])
        self.assertEqual(len(deps), 2)
        self.assertEqual(deps[0].id, "a")
        self.assertTrue(deps[0].is_required)
        self.assertFalse(deps[1].is_required)

    def test_parse_dependencies_string_shorthand(self):
        """社区常见的纯字符串简写应被容忍并视为必选。"""
        deps = parse_dependencies(["a", "b"])
        self.assertEqual([d.id for d in deps], ["a", "b"])
        self.assertTrue(all(d.is_required for d in deps))

    def test_parse_dependencies_edge_cases(self):
        self.assertEqual(parse_dependencies(None), [])
        self.assertEqual(parse_dependencies([]), [])
        self.assertEqual(parse_dependencies(123), [])

    def test_parse_supported_os_defaults_to_all(self):
        # 官方语义：未声明 = 支持全部平台
        self.assertEqual(parse_supported_os(None), ["Windows", "OSX", "Linux"])
        self.assertEqual(parse_supported_os([]), ["Windows", "OSX", "Linux"])

    def test_parse_supported_os_explicit(self):
        self.assertEqual(parse_supported_os(["Windows"]), ["Windows"])

    def test_load_manifest_from_disk(self):
        root = self.make_v1()
        layout = detect_layout(root)
        info, warnings = load_plugin_manifest(
            layout.plugins_dir / "classisland.example.dependent" / "manifest.yml"
        )
        self.assertIsNotNone(info)
        self.assertEqual(info.id, "classisland.example.dependent")
        self.assertEqual(len(info.dependencies), 1)
        self.assertEqual(info.dependencies[0].id, "classisland.example.basic")
        self.assertEqual(warnings, [])

    def test_scan_plugins_reads_state_markers(self):
        root = self.make_v1()
        infos = scan_plugins(detect_layout(root))
        by_id = {info.id: info for info in infos}

        self.assertTrue(by_id["classisland.example.basic"].enabled)
        self.assertFalse(by_id["classisland.example.disabled"].enabled)
        self.assertFalse(by_id["classisland.example.disabled"].uninstalling)

    def test_scan_detects_uninstall_marker(self):
        root = self.make_v1()
        make_plugin(
            detect_layout(root).plugins_dir, "x.one", uninstall=True
        )
        infos = scan_plugins(detect_layout(root))
        by_id = {i.id: i for i in infos}
        self.assertTrue(by_id["x.one"].uninstalling)

    def test_missing_manifest_is_skipped(self):
        root = self.make_v1()
        (detect_layout(root).plugins_dir / "broken").mkdir(parents=True)
        infos = scan_plugins(detect_layout(root))
        self.assertNotIn("broken", {i.id for i in infos})


class TestDependencyClosure(CIPackerTestCase):
    def test_missing_hard_dependency_detected(self):
        root = self.make_v1()
        infos = scan_plugins(detect_layout(root))
        issues = check_dependencies(infos)
        self.assertTrue(issues, "应检出缺失的硬依赖")
        messages = " ".join(i.message for i in issues)
        self.assertIn("classisland.example.not-installed", messages)

    def test_satisfied_dependency_no_issue(self):
        root = self.make_v1()
        infos = scan_plugins(detect_layout(root))
        issues = check_dependencies(infos)
        # dependent -> basic 已满足，不应报缺
        self.assertFalse(
            any(
                i.plugin_id == "classisland.example.dependent"
                and i.severity == "error"
                for i in issues
            )
        )

    def test_dependency_index(self):
        root = self.make_v1()
        infos = scan_plugins(detect_layout(root))
        index = build_dependency_index(infos)
        self.assertIn("classisland.example.basic", index)
        self.assertIn(
            "classisland.example.dependent", index["classisland.example.basic"]
        )

    def test_api_incompatibility_across_major(self):
        """插件 apiVersion 主版本与目标 CI 主版本不同 -> 报不兼容。"""
        root = self.make_v1()
        make_plugin(detect_layout(root).plugins_dir, "x.new", api_version="1.0.0.0")
        infos = scan_plugins(detect_layout(root))
        issues = check_api_compatibility(infos, target_ci_version="2.0.0.0")
        self.assertTrue(issues, "跨主版本应报不兼容")
        self.assertTrue(any(i.plugin_id == "x.new" for i in issues))

    def test_api_compatible_same_major(self):
        root = self.make_v1()
        infos = scan_plugins(detect_layout(root))
        issues = check_api_compatibility(infos, target_ci_version="2.5.0.0")
        self.assertFalse(any(i.plugin_id == "classisland.example.basic" for i in issues))


if __name__ == "__main__":
    unittest.main()
