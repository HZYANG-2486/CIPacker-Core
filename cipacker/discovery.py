"""目录遍历与文件收集。

本模块只消费 :class:`~cipacker.layout.Layout` 提供的路径与排除规则，
自身不做任何结构判定。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import logui
from .layout import (
    Layout,
    is_excluded_dir,
    is_excluded_file,
    should_include_backups,
)


@dataclass(frozen=True)
class DiscoveredFile:
    """遍历发现的一个文件。"""

    rel: str  # 相对数据根的 POSIX 路径
    size: int


def iter_files(
    layout: Layout,
    include_backups: bool = False,
) -> list[DiscoveredFile]:
    """遍历数据根，返回应参与迁移的文件列表。

    :param layout: 目标布局（决定数据根与排除规则）。
    :param include_backups: 是否包含 ``Backups/`` 目录。
    :return: 按相对路径排序的文件列表（保证确定性）。
    """
    app_root = layout.app_root
    if not app_root.is_dir():
        return []

    collected: list[DiscoveredFile] = []

    for dirpath, dirnames, filenames in os.walk(app_root):
        current = Path(dirpath)
        try:
            rel_dir = current.relative_to(app_root)
        except ValueError:
            dirnames[:] = []
            continue

        rel_parts = rel_dir.parts

        # 目录排除：原地裁剪 dirnames 以阻止 os.walk 下钻
        if is_excluded_dir(rel_parts, layout.kind):
            dirnames[:] = []
            continue

        # Backups/ 受开关控制
        if rel_parts and should_include_backups(rel_parts) and not include_backups:
            dirnames[:] = []
            continue

        # 排序 dirnames 以保证遍历顺序稳定
        dirnames.sort()
        for name in sorted(filenames):
            if is_excluded_file(rel_parts + (name,), name):
                continue

            full = current / name

            # 符号链接一律跳过：os.walk 不会下钻符号链接目录，但会把符号链接
            # 文件列入 filenames，open() 会跟随链接读到宿主机任意文件
            # （例如指向 /etc/shadow），导致敏感内容被打进迁移包。
            if full.is_symlink():
                logui.warn(f"跳过符号链接（不参与迁移）: {full}")
                continue
            full = current / name
            rel = (Path(*rel_parts) / name).as_posix() if rel_parts else name
            try:
                size = full.stat().st_size
            except OSError:
                logui.warn(f"无法读取文件大小，已跳过: {full}")
                continue
            collected.append(DiscoveredFile(rel=rel, size=size))

    collected.sort(key=lambda item: item.rel)
    return collected


def total_bytes(files: list[DiscoveredFile]) -> int:
    return sum(item.size for item in files)


def describe_tree(layout: Layout, include_backups: bool = False) -> list[str]:
    """生成可用于日志展示的「数据根一级条目」清单。"""
    app_root = layout.app_root
    lines: list[str] = []
    if not app_root.is_dir():
        return lines

    try:
        children = sorted(app_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return lines

    for child in children:
        rel_parts = (child.name,)
        if is_excluded_dir(rel_parts, layout.kind):
            lines.append(f"  - {child.name}/  (排除)")
            continue
        if child.name == "Backups":
            mark = "" if include_backups else "  (排除，--include-backups 可包含)"
            lines.append(f"  - {child.name}/{mark}")
            continue
        if child.is_dir():
            lines.append(f"  - {child.name}/")
        else:
            lines.append(f"  - {child.name}")

    return lines
