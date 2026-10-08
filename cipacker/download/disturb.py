"""disturb / disturb-net6 数据源，面向 ClassIsland 1.x。

这两条源托管在官方下载站（AList 驱动）上，索引为 JSON。

**关键坑**（旧版代码注释里也提到过，此处固化为常量与回退链）：
根路径 ``/classisland/disturb/index.json`` 会返回 AList 的 **HTML 目录页**，
必须使用 ``/d/`` 或 ``/p/`` 前缀的分发路径才能拿到真正的 JSON。
"""

from __future__ import annotations

import re

from .transfer import fetch_json

_CDN = "https://get.classisland.tech"
_BUCKET = "ClassIsland-Ningbo-S3"

#: 索引 URL 回退链（``/d/`` 优先）。
DISTURB_INDEX_URLS = (
    f"{_CDN}/d/{_BUCKET}/classisland/disturb/index.json",
    f"{_CDN}/p/{_BUCKET}/classisland/disturb/index.json",
    f"{_CDN}/classisland/disturb/index.json",
)

DISTURB_NET6_INDEX_URLS = (
    f"{_CDN}/d/{_BUCKET}/classisland/disturb-net6/index.json",
    f"{_CDN}/p/{_BUCKET}/classisland/disturb-net6/index.json",
    f"{_CDN}/classisland/disturb-net6/index.json",
)

DISTURB_SOURCES = {
    "disturb": DISTURB_INDEX_URLS,
    "disturb-net6": DISTURB_NET6_INDEX_URLS,
}

#: CI 1.x 产物变体优先级。
VARIANT_ORDER = (
    "windows_x64_full_singleFile",
    "windows_x64_trimmed_singleFile",
    "windows_x64_full_folder",
    "windows_x64_selfContained_folder",
)


def _version_key(version: str) -> tuple[int, ...]:
    """把版本号转为可排序数字元组。"""
    parts = re.findall(r"\d+", str(version))
    return tuple(int(part) for part in parts) or (0,)


def _index_urls(source: str, version: str | None = None) -> tuple[str, ...]:
    base_urls = DISTURB_SOURCES.get(source, DISTURB_INDEX_URLS)
    if version is None:
        return base_urls
    return tuple(
        url.replace("/index.json", f"/{version}/index.json") for url in base_urls
    )


def fetch_versions(source: str = "disturb") -> list[dict]:
    """获取某条 disturb 源的可用版本列表。"""
    for url in _index_urls(source):
        data = fetch_json(url)
        if isinstance(data, dict):
            versions = data.get("Versions")
            if isinstance(versions, list):
                return [item for item in versions if isinstance(item, dict)]
    return []


def fetch_all_versions() -> list[tuple[str, str]]:
    """汇总两条 disturb 源的版本，返回 ``[(version, source), ...]`` 去重列表。"""
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for source in ("disturb", "disturb-net6"):
        for item in fetch_versions(source):
            version = item.get("Version")
            if not version or version in seen:
                continue
            seen.add(version)
            result.append((version, source))
    return result


def fetch_version_info(version: str, source: str | None = None) -> dict | None:
    """获取某版本的下载信息（含各变体与镜像）。"""
    sources = [source] if source else ["disturb", "disturb-net6"]
    for candidate in sources:
        if candidate is None:
            continue
        for url in _index_urls(candidate, version):
            data = fetch_json(url)
            if isinstance(data, dict) and data.get("DownloadInfos"):
                return data
    return None


def select_variant(download_infos: dict) -> dict | None:
    """按优先级选出合适的产物变体。"""
    for variant in VARIANT_ORDER:
        info = download_infos.get(variant)
        if isinstance(info, dict):
            return info

    for key, info in download_infos.items():
        if "windows_x64" in key and isinstance(info, dict):
            return info

    return None


def latest_stable_version(source: str = "disturb") -> str | None:
    """取某条源的最新稳定版版本号。"""
    versions = fetch_versions(source)
    stable = [
        item
        for item in versions
        if isinstance(item.get("Channels"), list) and "stable" in item["Channels"]
    ]
    if not stable:
        return None
    stable.sort(key=lambda item: _version_key(item.get("Version", "0")))
    return stable[-1].get("Version")


def has_stable_channel(version_info: dict) -> bool:
    channels = version_info.get("Channels")
    return isinstance(channels, list) and "stable" in channels


def mirror_entries(variant: dict) -> list[tuple[str, str]]:
    """提取某变体的可用镜像 ``[(名称, url), ...]``。"""
    urls = variant.get("ArchiveDownloadUrls") or {}
    if not isinstance(urls, dict):
        return []
    return [
        (name, url)
        for name, url in urls.items()
        if url and name != "DeployMethod" and isinstance(url, str)
    ]


def mirror_probe_url(mirror_url: str) -> str:
    """为镜像构造用于延迟探测的 URL（取目录而非文件）。"""
    if "/download/" in mirror_url:
        return mirror_url.split("/download/")[0]
    return mirror_url.rsplit("/", 1)[0]
