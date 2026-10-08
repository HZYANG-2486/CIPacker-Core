"""GitHub Releases 数据源。

旧版有 ``fetch_latest_release`` / ``fetch_release_by_version`` /
``fetch_all_github_releases`` 三个几乎逐行重复的函数。这里统一为一个
:func:`fetch_releases`，其余都是它的薄封装。
"""

from __future__ import annotations

import platform
from urllib.parse import quote

from .transfer import fetch_json

API_ROOT = "https://api.github.com/repos/ClassIsland/ClassIsland"
RELEASES_URL = f"{API_ROOT}/releases"


def _load(url: str) -> object | None:
    return fetch_json(url, timeout=20.0)


def fetch_releases(per_page: int = 30) -> list[dict]:
    """获取 Releases 列表（按发布时间倒序，GitHub 默认顺序）。"""
    data = _load(f"{RELEASES_URL}?per_page={per_page}")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def fetch_latest_release() -> dict | None:
    """获取最新正式发布。"""
    data = _load(f"{RELEASES_URL}/latest")
    return data if isinstance(data, dict) else None


def fetch_release_by_version(version: str) -> dict | None:
    """按版本号或 tag 获取发布。会自动尝试 ``v`` 前缀。"""
    clean = str(version).strip()
    for tag in (clean, f"v{clean}", f"V{clean}"):
        data = _load(f"{RELEASES_URL}/tags/{quote(tag)}")
        if isinstance(data, dict) and data.get("assets"):
            return data
    return None


def get_system_os() -> str:
    if platform.system() == "Windows":
        return "windows"
    if platform.system() == "Darwin":
        return "macos"
    return "linux"


def get_system_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "x64"
    if machine in ("x86", "i386", "i686"):
        return "x86"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return "x64"


def _score_asset(name: str, target_os: str, target_arch: str) -> int:
    """为候选资源打分，选出最匹配当前平台的那个。"""
    lowered = name.lower()
    score = 0

    if target_os in lowered:
        score += 1000
    if target_arch in lowered:
        score += 500

    # 文件夹形态比单文件更适合作为可迁移的安装
    if "folder" in lowered:
        score += 100
    elif "singlefile" in lowered:
        score += 80

    if "selfcontained" in lowered:
        score += 50
    elif "trimmed" in lowered:
        score += 30

    if lowered.endswith(".zip"):
        score += 20
    elif lowered.endswith(".7z"):
        score += 10

    return score


def select_best_asset(release: dict) -> dict | None:
    """从发布的资源中选出最适合当前平台的一个。"""
    assets = release.get("assets") or []
    if not assets:
        return None

    def is_source(asset_name: str) -> bool:
        lowered = asset_name.lower()
        return "source code" in lowered or "source_code" in lowered

    candidates = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and asset.get("name")
        and asset.get("browser_download_url")
        and not is_source(asset.get("name", ""))
    ]
    if not candidates:
        return None

    target_os = get_system_os()
    target_arch = get_system_arch()

    candidates.sort(
        key=lambda asset: _score_asset(asset["name"], target_os, target_arch),
        reverse=True,
    )

    best = candidates[0]
    return {
        "name": best["name"],
        "browser_download_url": best["browser_download_url"],
        "size": best.get("size", 0),
    }


def version_from_tag(tag: str) -> str:
    """从 tag 提取纯版本号。"""
    return str(tag).lstrip("vV").strip()
