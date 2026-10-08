"""分层、确定性打包、指纹与往返测试（P3 / P4）。

这是本项目的核心承诺，因此测试最为密集：

* **确定性**：同一目录、不同时刻打包，字节完全一致
* **指纹**：内容指纹与包时间戳解耦；自引用悖论被绕开
* **分层**：各层文件集合精确；软依赖给出警告
* **往返**：pack → unpack → pack 得到逐字节相同的归档
"""

from __future__ import annotations

import hashlib
import time
import unittest
import zipfile
from pathlib import Path

from cipacker.archive import ArchiveFormat, read_archive, read_manifest
from cipacker.discovery import iter_files
from cipacker.fingerprint import compute_content_fingerprint, hash_file
from cipacker.layers import (
    LayerName,
    parse_layer_names,
    resolve_selection,
)
from cipacker.layout import detect_layout
from cipacker.models import DETERMINISTIC_TIMESTAMP, MANIFEST_NAME
from cipacker.pack import (
    build_manifest,
    collect_pack_items,
    deterministic_manifest,
    write_package,
)
from cipacker.unpack import build_plan, execute_plan

try:
    from .base import CIPackerTestCase
except ImportError:
    from base import CIPackerTestCase  # type: ignore


def _pack(root: Path, output: Path, layers: str = "all", **kw):
    layout = detect_layout(root)
    selection = resolve_selection(parse_layer_names(layers))
    discovered = iter_files(layout, include_backups=kw.pop("include_backups", False))
    items, redacted, warnings = collect_pack_items(layout, selection, discovered, **kw)
    manifest = build_manifest(
        layout,
        selection,
        include_backups=False,
        deterministic=True,
        redacted=redacted,
    )
    deterministic_manifest(manifest)
    entries, fingerprint, sidecar = write_package(items, output, manifest)
    return layout, items, manifest, entries, fingerprint, sidecar, warnings


class TestLayers(CIPackerTestCase):
    def test_parse_all(self):
        self.assertEqual(
            set(parse_layer_names("all")),
            {
                LayerName.SETTINGS,
                LayerName.PROFILES,
                LayerName.PLUGINS,
                LayerName.PLUGIN_CONFIGS,
                LayerName.AUTOMATION,
            },
        )

    def test_parse_none_is_all(self):
        self.assertEqual(set(parse_layer_names(None)), set(parse_layer_names("all")))

    def test_parse_unknown_raises(self):
        with self.assertRaises(ValueError):
            parse_layer_names("settings,bogus")

    def test_soft_dependency_warning(self):
        sel = resolve_selection(parse_layer_names("plugin-configs"))
        self.assertTrue(sel.warnings, "plugin-configs 未带 plugins 应给出警告")

    def test_layers_select_exact_files(self):
        root = self.make_v1()
        _, items, _, _, _, _, _ = _pack(root, self.path("slim.zip"), "settings")
        self.assertEqual([i.rel_posix for i in items], ["Settings.json"])

    def test_profiles_layer_excludes_settings(self):
        root = self.make_v1()
        _, items, _, _, _, _, _ = _pack(root, self.path("p.zip"), "profiles")
        rels = {i.rel_posix for i in items}
        self.assertNotIn("Settings.json", rels)
        self.assertIn("Profiles/ClassPlans/cp-1.json", rels)
        self.assertTrue(all(r.startswith("Profiles/") for r in rels))

    def test_settings_profiles_combined(self):
        root = self.make_v1()
        _, items, _, _, _, _, _ = _pack(root, self.path("sp.zip"), "settings,profiles")
        rels = {i.rel_posix for i in items}
        self.assertIn("Settings.json", rels)
        self.assertIn("Profiles/ClassPlans/cp-1.json", rels)
        self.assertNotIn("Plugins/classisland.example.basic/manifest.yml", rels)


class TestDeterminism(CIPackerTestCase):
    def test_byte_identical_across_time(self):
        root = self.make_v1()
        _pack(root, self.path("a.zip"))
        time.sleep(1.2)  # 制造 mtime 差异
        _pack(root, self.path("b.zip"))

        a = (self.path("a.zip")).read_bytes()
        b = (self.path("b.zip")).read_bytes()
        self.assertEqual(
            hashlib.sha256(a).hexdigest(),
            hashlib.sha256(b).hexdigest(),
            "同一目录两次打包必须字节一致",
        )

    def test_all_entries_use_fixed_timestamp(self):
        root = self.make_v1()
        out = self.path("t.zip")
        _pack(root, out)
        with zipfile.ZipFile(out) as zf:
            for info in zf.infolist():
                self.assertEqual(
                    info.date_time,
                    (1980, 1, 1, 0, 0, 0),
                    f"{info.filename} 的时间戳应为固定值",
                )

    def test_entries_sorted(self):
        """除开头的 __manifest__.json 外，其余条目应按路径升序。"""
        root = self.make_v1()
        out = self.path("s.zip")
        _pack(root, out)
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        # 清单固定在首位（便于不解压即可读取）
        self.assertEqual(names[0], MANIFEST_NAME)
        self.assertEqual(names[1:], sorted(names[1:]))

    def test_manifest_timestamp_normalized(self):
        root = self.make_v1()
        out = self.path("m.zip")
        _, _, manifest, _, _, _, _ = _pack(root, out)
        self.assertEqual(manifest.created_at, "1970-01-01T00:00:00+00:00")
        self.assertEqual(DETERMINISTIC_TIMESTAMP[0], 1980)

    def test_preserve_mtime_breaks_determinism(self):
        """显式关闭确定性时，输出应包含真实 mtime。"""
        root = self.make_v1()
        layout = detect_layout(root)
        selection = resolve_selection(parse_layer_names("all"))
        discovered = iter_files(layout)
        items, _, _ = collect_pack_items(layout, selection, discovered)
        manifest = build_manifest(
            layout, selection, include_backups=False, deterministic=False
        )
        out = self.path("real.zip")
        write_package(items, out, manifest, deterministic=False)
        with zipfile.ZipFile(out) as zf:
            stamps = {i.date_time for i in zf.infolist()}
        self.assertTrue(
            any(s != (1980, 1, 1, 0, 0, 0) for s in stamps),
            "非确定性模式不应把所有时间戳压平",
        )


class TestFingerprint(CIPackerTestCase):
    def test_fingerprint_independent_of_archive_timestamp(self):
        """指纹基于内容清单，与打包时刻无关（绕开自引用悖论）。"""
        root = self.make_v1()
        _, _, _, _, fp1, _, _ = _pack(root, self.path("f1.zip"))
        time.sleep(1.1)
        _, _, _, _, fp2, _, _ = _pack(root, self.path("f2.zip"))
        self.assertEqual(fp1.content_sha256, fp2.content_sha256)

    def test_fingerprint_changes_with_content(self):
        root = self.make_v1()
        _, _, _, _, fp_before, _, _ = _pack(root, self.path("x1.zip"))
        (root / "Settings.json").write_text('{"changed": true}', encoding="utf-8")
        _, _, _, _, fp_after, _, _ = _pack(root, self.path("x2.zip"))
        self.assertNotEqual(fp_before.content_sha256, fp_after.content_sha256)

    def test_compute_content_fingerprint_is_order_independent(self):
        """输入顺序不同但集合相同 -> 指纹一致。"""
        from cipacker.models import FileEntry, LayerName

        root = self.make_v1()
        entries = [i.rel for i in iter_files(detect_layout(root))]
        file_entries = []
        for rel in entries:
            p = root / rel
            file_entries.append(
                FileEntry(
                    path=rel,
                    size=p.stat().st_size,
                    sha256=hash_file(p),
                    layer=LayerName.SETTINGS,
                )
            )
        self.assertEqual(
            compute_content_fingerprint(file_entries),
            compute_content_fingerprint(list(reversed(file_entries))),
        )

    def test_sidecar_written(self):
        root = self.make_v1()
        out = self.path("sc.zip")
        _, _, _, _, fp, sidecar, _ = _pack(root, out)
        self.assertIsNotNone(sidecar)
        self.assertTrue(sidecar.exists())
        # sha256sum 风格：`<hash>  <filename>`
        content = sidecar.read_text(encoding="utf-8").strip()
        self.assertTrue(content.startswith(fp.archive_sha256))
        self.assertIn(out.name, content)


class TestRoundTrip(CIPackerTestCase):
    def test_pack_unpack_pack_identical(self):
        src = self.make_v1("src")
        out1 = self.path("first.zip")
        _, _, _, _, fp1, _, _ = _pack(src, out1)

        restore = self.path("restore")
        restore.mkdir()
        layout = detect_layout(restore)
        plan = build_plan(out1, layout)
        execute_plan(out1, layout, plan, make_backup=False)

        out2 = self.path("second.zip")
        _, _, _, _, fp2, _, _ = _pack(restore, out2)

        self.assertEqual(
            fp1.content_sha256,
            fp2.content_sha256,
            "pack→unpack→pack 的内容指纹必须一致",
        )
        self.assertEqual(
            hashlib.sha256(out1.read_bytes()).hexdigest(),
            hashlib.sha256(out2.read_bytes()).hexdigest(),
            "往返后归档应逐字节相同",
        )

    def test_roundtrip_preserves_file_set(self):
        src = self.make_v1("src2")
        out1 = self.path("rt.zip")
        _, items, _, _, _, _, _ = _pack(src, out1)

        restore = self.path("restore2")
        restore.mkdir()
        layout = detect_layout(restore)
        execute_plan(out1, layout, build_plan(out1, layout), make_backup=False)

        original = {i.rel_posix for i in items}
        restored = {i.rel for i in iter_files(detect_layout(restore))}
        self.assertEqual(original, restored)


class TestArchiveReading(CIPackerTestCase):
    def test_own_v3_detected(self):
        root = self.make_v1()
        out = self.path("v3.zip")
        _pack(root, out)
        info = read_archive(out)
        self.assertIs(info.format, ArchiveFormat.OWN_V3)
        self.assertIsNotNone(info.manifest)

    def test_manifest_embedded(self):
        root = self.make_v1()
        out = self.path("mf.zip")
        _pack(root, out)
        with zipfile.ZipFile(out) as zf:
            self.assertIn(MANIFEST_NAME, zf.namelist())
        manifest = read_manifest(out)
        self.assertEqual(manifest.file_count, len(manifest.files))

    def test_plain_zip_without_manifest(self):
        root = self.make_v1()
        out = self.path("plain.zip")
        _pack(root, out)
        # 去掉清单，模拟"官方风格"包
        stripped = self.path("stripped.zip")
        with zipfile.ZipFile(out) as zin, zipfile.ZipFile(
            stripped, "w", zipfile.ZIP_DEFLATED
        ) as zout:
            for info in zin.infolist():
                if info.filename == MANIFEST_NAME:
                    continue
                zout.writestr(info, zin.read(info.filename))
        info = read_archive(stripped)
        self.assertIsNone(info.manifest)
        self.assertNotEqual(info.format, ArchiveFormat.OWN_V3)


if __name__ == "__main__":
    unittest.main()
