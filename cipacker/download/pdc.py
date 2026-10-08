"""官方分发中心（PDC / Distribution API）数据源，面向 ClassIsland 2.x。

端点（社区下载站 ``get.classisland.tech`` 背后的 Phainon Distribution Center）：

- ``/api/v1/public/distributions/web``                  渠道列表
- ``/api/v1/public/distributions/web/{vid}/{os}_{arch}_{variant}``  具体产物

产物命名形如 ``windows_x64_selfContained_folder``，与
``ClassIsland.Core/AppBase.cs`` 中的 ``AppSubChannel`` 构造规则一致。
"""

from __future__ import annotations

from ..download.transfer import fetch_json

DIST_BASE = "https://distribution.classisland.tech/api/v1/public/distributions/web"


def fetch_distribution_channels() -> dict | None:
    """获取渠道列表。

    :return: ``{"channels": {...}, "default_channel": str}`` 或 None
    """
    data = fetch_json(DIST_BASE, timeout=15.0)
    if not isinstance(data, dict):
        return None
    if data.get("code", 0) != 0:
        return None

    content = data.get("content") or {}
    raw_channels = content.get("channels") or {}
    if not isinstance(raw_channels, dict):
        return None

    channels: dict[str, dict] = {}
    for channel_id, info in raw_channels.items():
        if not isinstance(info, dict):
            continue
        channels[channel_id] = {
            "latestVersionId": info.get("latestVersionId"),
            "latestVersion": info.get("latestVersion"),
            "channelName": info.get("channelName"),
            "channelDescription": info.get("channelDescription"),
        }

    return {
        "channels": channels,
        "default_channel": content.get("defaultChannel"),
    }


def build_distribution_variants(target_os: str, target_arch: str) -> list[str]:
    """按优先级生成产物变体名列表。"""
    os_lower = (target_os or "").lower()
    if os_lower == "windows":
        return ["selfContained_folder", "full_folder", "full_singleFile"]
    return ["selfContained_folder", "full_folder"]


def fetch_distribution_asset(
    version_id: str,
    target_os: str,
    target_arch: str,
    variant: str = "selfContained_folder",
) -> dict | None:
    """获取具体产物的下载信息（含 SHA512）。"""
    url = f"{DIST_BASE}/{version_id}/{target_os}_{target_arch}_{variant}"
    data = fetch_json(url, timeout=15.0)
    if not isinstance(data, dict) or data.get("code", 0) != 0:
        return None

    content = data.get("content") or {}
    archive_url = content.get("archiveUrl")
    if not archive_url:
        return None

    name = archive_url.split("/")[-1].split("?")[0]

    return {
        "url": archive_url,
        "sha512": content.get("archiveSHA512"),
        "version": content.get("version"),
        "name": name,
        "variant": variant,
    }


def resolve_latest(
    target_os: str,
    target_arch: str,
    channel_id: str | None = None,
) -> dict | None:
    """解析指定渠道的最新产物，自动尝试各变体。"""
    channels_info = fetch_distribution_channels()
    if not channels_info:
        return None

    channels = channels_info.get("channels") or {}
    chosen_id = channel_id or channels_info.get("default_channel")
    if not chosen_id or chosen_id not in channels:
        return None

    channel = channels[chosen_id]
    version_id = channel.get("latestVersionId")
    if not version_id:
        return None

    for variant in build_distribution_variants(target_os, target_arch):
        asset = fetch_distribution_asset(version_id, target_os, target_arch, variant)
        if asset:
            asset["channel"] = chosen_id
            asset["latest_version"] = channel.get("latestVersion")
            return asset

    return None
