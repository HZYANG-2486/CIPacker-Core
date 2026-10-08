"""合成 ClassIsland 数据目录，供测试使用。

不依赖任何真实 CI 安装，纯标准库生成。覆盖：

* V1 布局（``Settings.json`` 直接位于根）
* V2 布局（``AppPackageRoot/app-<ver>-<n>/`` + ``data/``）
* 插件（启用 / 禁用 / 待卸载 / 缺依赖 / API 不兼容）
* 敏感字段（口令、账号、绝对路径）
* 应被排除的目录（Cache / Logs / Temp / Config/PluginsIndex）

所有函数返回创建的根目录 :class:`pathlib.Path`，调用方负责清理。
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "write_json",
    "make_plugin",
    "make_v1_tree",
    "make_v2_tree",
    "make_dirty_tree",
    "make_sensitive_tree",
    "make_big_tree",
]


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def write_json(path: Path, data: object) -> Path:
    """写一份 UTF-8 无 BOM 的 JSON（与 ConfigureFileHelper 行为接近）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 插件
# --------------------------------------------------------------------------- #
DEFAULT_SETTINGS = {
    "SelectedProfile": "d2f1e5e2-0000-4000-8000-000000000001",
    "IsAutoUpdateEnabled": True,
    "Theme": "Light",
    "LastUpdateCheck": "2025-01-01T08:00:00+08:00",
}


def make_plugin(
    plugins_dir: Path,
    plugin_id: str,
    *,
    name: str | None = None,
    version: str = "1.0.0",
    api_version: str = "2.0.0.0",
    enabled: bool = True,
    uninstall: bool = False,
    dependencies: Iterable[dict] | None = None,
    platforms: Iterable[str] | None = None,
    payload: bytes = b"",
) -> Path:
    """在 ``plugins_dir`` 下生成一个插件目录。

    返回插件目录路径。``payload`` 会写进 ``Plugin.dll``，用于制造体积差异。
    """
    pdir = plugins_dir / plugin_id
    pdir.mkdir(parents=True, exist_ok=True)

    lines = [
        f"id: {plugin_id}",
        f"name: {name or plugin_id}",
        f"description: 合成插件 {name or plugin_id}",
        f"version: {version}",
        f"apiVersion: {api_version}",
        "author: synth",
        "entranceAssembly: Plugin.dll",
        "url: https://example.invalid/plugin",
    ]
    deps = list(dependencies or [])
    if deps:
        lines.append("dependencies:")
        for dep in deps:
            lines.append(f"  - id: {dep['id']}")
            if "version" in dep:
                lines.append(f"    version: {dep['version']}")
    if platforms:
        lines.append("supportedOSPlatforms:")
        for p in platforms:
            lines.append(f"  - {p}")

    write_text(pdir / "manifest.yml", "\n".join(lines) + "\n")
    (pdir / "Plugin.dll").write_bytes(payload or f"dll:{plugin_id}".encode())

    if not enabled:
        (pdir / ".disabled").write_text("")
    if uninstall:
        (pdir / ".uninstall").write_text("")

    return pdir


# --------------------------------------------------------------------------- #
# 布局构造
# --------------------------------------------------------------------------- #
def _populate_data_root(
    data_root: Path,
    *,
    plugins: bool = True,
    plugin_configs: bool = True,
    automation: bool = True,
    backups: bool = False,
    noise: bool = True,
) -> None:
    """往一个"数据根"里填内容（V1 的根 == V2 的 data/）。"""
    write_json(data_root / "Settings.json", DEFAULT_SETTINGS)

    profiles = data_root / "Profiles"
    for pid, pname in (
        ("d2f1e5e2-0000-4000-8000-000000000001", "高二(3)班"),
        ("d2f1e5e2-0000-4000-8000-000000000002", "高三(1)班"),
    ):
        write_json(
            profiles / f"{pid}.json",
            {"Id": pid, "Name": pname, "ClassPlanId": "cp-1"},
        )
    write_json(
        profiles / "ClassPlans" / "cp-1.json",
        {
            "Id": "cp-1",
            "Name": "日常课表",
            "TimeLayoutId": "tl-1",
            "Classes": [{"SubjectId": "s-math", "DayOfWeek": 1}],
        },
    )
    write_json(profiles / "TimeLayouts" / "tl-1.json", {"Id": "tl-1", "Name": "夏季作息"})
    write_json(profiles / "Subjects" / "s-math.json", {"Id": "s-math", "Name": "数学"})

    if plugins:
        pdir = data_root / "Plugins"
        make_plugin(pdir, "classisland.example.basic", name="基础插件")
        make_plugin(pdir, "classisland.example.disabled", name="被禁用的插件", enabled=False)
        make_plugin(
            pdir,
            "classisland.example.dependent",
            name="依赖插件",
            dependencies=[{"id": "classisland.example.basic", "version": "1.0.0"}],
        )
        make_plugin(
            pdir,
            "classisland.example.orphan-dep",
            name="缺依赖插件",
            dependencies=[{"id": "classisland.example.not-installed", "version": "9.9.9"}],
        )

    if plugin_configs:
        cfg = data_root / "Config"
        write_json(cfg / "Plugins" / "classisland.example.basic" / "settings.json", {"Level": 2})
        write_json(
            cfg / "Plugins" / "classisland.example.ghost" / "settings.json",
            {"Level": 9},  # 孤儿配置：对应插件不存在
        )
        if noise:
            # 插件索引缓存，属于"不应迁移"的内容
            write_json(cfg / "PluginsIndex" / "index.json", {"Generated": "cache"})

    if automation:
        write_json(
            data_root / "Config" / "Automation" / "Automations.json",
            {"Automations": [{"Id": "a-1", "Name": "上课响铃"}]},
        )

    if backups:
        write_json(
            data_root / "Backups" / "2025-01-01_120000_auto.zip.json",
            {"Kind": "placeholder"},
        )

    if noise:
        # 这些目录必须被排除
        write_text(data_root / "Logs" / "app-2025-01-01.log", "INFO boot\n" * 20)
        write_text(data_root / "Cache" / "thumb.bin", "x" * 512)
        write_text(data_root / "Temp" / "scratch.tmp", "tmp")


def make_v1_tree(root: Path) -> Path:
    """CI 1.x：数据直接位于给定根目录下。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    write_text(root / "ClassIsland.exe", "MZ fake binary")
    write_text(root / "ClassIsland.dll", "fake dll")
    _populate_data_root(root)
    return root


def make_v2_tree(root: Path) -> Path:
    """CI 2.x：``app-<version>-<n>/`` 为程序目录，``data/`` 才是数据根。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    version_dir = root / "app-2.0.0.0-1"
    version_dir.mkdir(parents=True, exist_ok=True)
    write_text(version_dir / "ClassIsland.exe", "MZ fake binary")
    write_text(version_dir / "ClassIsland.dll", "fake dll")
    write_text(version_dir / "version.txt", "2.0.0.0")

    data_root = root / "data"
    _populate_data_root(data_root)
    return root


def make_dirty_tree(root: Path) -> Path:
    """含损坏内容：非法 JSON、空文件。用于 doctor 规则测试。"""
    root = make_v1_tree(root)
    (root / "Settings.json").write_text("{ this is not json", encoding="utf-8")
    (root / "Profiles" / "broken.json").write_text("{{{", encoding="utf-8")
    (root / "Profiles" / "empty.json").write_text("", encoding="utf-8")
    return root


def make_sensitive_tree(root: Path) -> Path:
    """含敏感字段：口令、账号、绝对路径、手机号。"""
    root = make_v1_tree(root)
    write_json(
        root / "Config" / "Plugins" / "classisland.example.basic" / "settings.json",
        {
            "ServerHost": "https://example.invalid",
            "Username": "admin",
            "Password": "Sup3rSecret!",
            "ApiToken": "abc123def456ghi789",
            "WebhookUrl": "https://open.feishu.cn/open-apis/bot/v2/hook/aaa-bbb-ccc",
            "Proxy": "http://127.0.0.1:7890",
            "Phone": "13800138000",
            "IdCard": "110101199003074512",
            "LocalPath": "C:\\Users\\Administrator\\Documents\\ci",
            "PosixPath": "/home/administrator/.config/classisland",
        },
    )
    write_json(
        root / "Settings.json",
        {**DEFAULT_SETTINGS, "Token": "ghp_0123456789abcdefghijklmnopqrstuvwx"},
    )
    return root


def make_big_tree(root: Path, files: int = 60, size: int = 4096) -> Path:
    """体积较大的树，用于进度/大文件规则测试。"""
    root = make_v1_tree(root)
    blob = os.urandom(size)
    d = root / "Profiles" / "Bulk"
    for i in range(files):
        p = d / f"bulk-{i:04d}.bin"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)
    return root


# --------------------------------------------------------------------------- #
# 清理
# --------------------------------------------------------------------------- #
def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
