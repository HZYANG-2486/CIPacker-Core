"""下载子模块。

对外暴露统一入口，内部按数据源拆分：

- :mod:`~cipacker.download.transfer` — 传输层：断点续传、重试、校验
- :mod:`~cipacker.download.github`   — GitHub Releases
- :mod:`~cipacker.download.disturb`  — disturb / disturb-net6（CI 1.x）
- :mod:`~cipacker.download.pdc`      — 官方分发中心（CI 2.x）

区别于旧版：三个 release 抓取函数（几乎逐行重复）收敛为
:func:`~cipacker.download.github.fetch_releases` 单实现，
CI 1.x / 2.x 两条下载链合并到 :func:`install_ci` 一条路径。
"""

from __future__ import annotations

from pathlib import Path

from .github import (
    fetch_latest_release,
    fetch_release_by_version,
    fetch_releases,
    select_best_asset,
)
from .pdc import (
    build_distribution_variants,
    fetch_distribution_asset,
    fetch_distribution_channels,
)
from .sources import (
    DownloadSource,
    list_sources,
    ping_sources,
    select_source,
)
from .transfer import DownloadTask, download_file

__all__ = [
    "DownloadSource",
    "DownloadTask",
    "download_file",
    "list_sources",
    "ping_sources",
    "select_source",
    "fetch_releases",
    "fetch_latest_release",
    "fetch_release_by_version",
    "select_best_asset",
    "fetch_distribution_channels",
    "fetch_distribution_asset",
    "build_distribution_variants",
]


def install_ci(*args, **kwargs):
    """安装 ClassIsland（延迟导入以避免循环依赖）。"""
    from .installer import install_ci as _install

    return _install(*args, **kwargs)


def resolve_save_dir(target: Path) -> Path:
    """确保目标目录存在并返回。"""
    target.mkdir(parents=True, exist_ok=True)
    return target
