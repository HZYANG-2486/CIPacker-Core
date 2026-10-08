"""ClassIsland 安装编排。

旧版把 CI 1.x 与 2.x 写成两条各约 100 行的下载链，逻辑高度重复。
这里统一为「**解析产物 → 下载 → 解压 → 校验**」四步，版本差异
只在第一步的解析器上有分野。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import logui
from ..errors import DownloadError, PreconditionError
from ..extract import extract_ci_package
from ..layout import detect_layout
from . import github, pdc, sources
from .disturb import (
    fetch_all_versions,
    fetch_version_info,
    mirror_entries,
    mirror_probe_url,
    select_variant,
)
from .transfer import DownloadTask, download_file, probe_latency


@dataclass
class InstallRequest:
    """一次安装请求。"""

    target: Path
    version: str | None = None
    major_version: int | None = None
    source_key: str | None = None
    spoof_ua: bool = False
    verify: bool = True
    progress: object | None = None


@dataclass
class ResolvedAsset:
    """解析后的可下载产物。"""

    url: str
    name: str
    size: int = 0
    sha512: str | None = None
    sha256: str | None = None
    version: str | None = None
    source: str = ""
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 产物解析
# --------------------------------------------------------------------------- #


def _resolve_github(version: str | None) -> ResolvedAsset | None:
    release = (
        github.fetch_release_by_version(version)
        if version
        else github.fetch_latest_release()
    )
    if release is None:
        return None

    asset = github.select_best_asset(release)
    if asset is None:
        return None

    tag = release.get("tag_name") or version or ""
    return ResolvedAsset(
        url=asset["browser_download_url"],
        name=asset["name"],
        size=asset.get("size", 0),
        version=github.version_from_tag(tag) or version,
        source="github",
    )


def _resolve_pdc(version: str | None, major: int | None) -> ResolvedAsset | None:
    target_os = github.get_system_os()
    target_arch = github.get_system_arch()

    asset = pdc.resolve_latest(target_os, target_arch)
    if asset is None:
        return None

    # 指定了非最新版本时，PDC 无法直接取到，交给 GitHub
    if version and asset.get("latest_version") and version != asset["latest_version"]:
        return None

    return ResolvedAsset(
        url=asset["url"],
        name=asset["name"],
        sha512=asset.get("sha512"),
        version=asset.get("version") or asset.get("latest_version"),
        source="distribution",
    )


def _resolve_disturb(version: str, source_key: str) -> ResolvedAsset | None:
    info = fetch_version_info(version, source_key)
    if info is None:
        return None

    download_infos = info.get("DownloadInfos") or {}
    variant = select_variant(download_infos)
    if variant is None:
        return None

    mirrors = mirror_entries(variant)
    if not mirrors:
        return None

    warnings: list[str] = []
    chosen_name, chosen_url = mirrors[0]

    # 多镜像时测速择优
    if len(mirrors) > 1:
        scored: list[tuple[float, str, str]] = []
        for name, url in mirrors:
            latency = probe_latency(mirror_probe_url(url))
            if latency is not None:
                scored.append((latency, name, url))
        if scored:
            scored.sort(key=lambda item: item[0])
            _, chosen_name, chosen_url = scored[0]
        else:
            warnings.append("所有镜像均不可达，回退到首个镜像")

    name = chosen_url.split("/")[-1].split("?")[0]
    return ResolvedAsset(
        url=chosen_url,
        name=name,
        sha256=variant.get("ArchiveSHA256"),
        version=version,
        source=source_key,
        warnings=warnings,
    )


def resolve_asset(request: InstallRequest) -> ResolvedAsset:
    """解析出待下载的产物。

    按 ``显式指定源 → PDC → disturb → GitHub`` 的顺序尝试，
    每一层失败就落到下一层，保证在网络受限环境下也能装上。
    """
    version = request.version
    major = request.major_version

    attempts: list[tuple[str, Callable[[], Any]]] = []

    if request.source_key:
        source = sources.get_source(request.source_key)
        if source is None:
            raise DownloadError(f"未知的数据源: {request.source_key}")
        if source.applies_to == "1.x" and version:
            attempts.append(("disturb", lambda: _resolve_disturb(version or "", source.key)))
        elif source.key == "github":
            attempts.append(("github", lambda: _resolve_github(version)))
        elif source.key == "distribution":
            attempts.append(("distribution", lambda: _resolve_pdc(version, major)))
    else:
        if major != 1:
            attempts.append(("distribution", lambda: _resolve_pdc(version, major)))
        if version and (major == 1 or _is_1x(version)):
            for key in ("disturb", "disturb-net6"):

                def _make_disturb(
                    k: str = key, v: str = version or ""
                ) -> Callable[[], Any]:
                    return lambda: _resolve_disturb(v, k)

                attempts.append((key, _make_disturb()))
        attempts.append(("github", lambda: _resolve_github(version)))

    errors: list[str] = []
    for label, resolver in attempts:
        try:
            asset = resolver()
        except Exception as exc:  # noqa: BLE001 - 逐层回退
            errors.append(f"{label}: {exc}")
            continue
        if asset is not None:
            return asset
        errors.append(f"{label}: 未找到可用产物")

    raise DownloadError(
        "无法解析出可下载的 ClassIsland 产物:\n  " + "\n  ".join(errors),
        hint="可用 --source 显式指定数据源，或用 --ci-version 指定版本",
    )


def _is_1x(version: str) -> bool:
    return str(version).lstrip("vV").startswith("1.")


# --------------------------------------------------------------------------- #
# 安装
# --------------------------------------------------------------------------- #


def install_ci(
    request: InstallRequest,
    *,
    progress_callback=None,
) -> bool:
    """下载并安装 ClassIsland 到目标目录。

    :return: 是否安装成功。
    """
    target = request.target
    target.mkdir(parents=True, exist_ok=True)

    asset = resolve_asset(request)

    logui.out(f"数据源: {asset.source}")
    logui.out(f"准备下载: {asset.name}")
    if asset.version:
        logui.out(f"版本: {asset.version}")
    logui.out(f"下载地址: {asset.url}")
    for warning in asset.warnings:
        logui.warn(warning)

    task = DownloadTask(
        url=asset.url,
        filename=asset.name,
        size=asset.size,
        sha512=asset.sha512 if request.verify else None,
        sha256=asset.sha256 if request.verify else None,
        expect_zip=True,
    )

    result = download_file(
        task,
        target,
        spoof_ua=request.spoof_ua,
        progress=progress_callback,
    )
    for warning in result.warnings:
        logui.warn(warning)

    logui.out("正在解压…")
    try:
        if not extract_ci_package(result.path, target):
            raise PreconditionError("解压后的目录不是有效的 ClassIsland 安装")
    finally:
        result.path.unlink(missing_ok=True)

    layout = detect_layout(target)
    if not layout.is_valid_install():
        raise PreconditionError(
            "安装校验未通过",
            hint=f"请检查目录结构: {layout.app_root}",
        )

    logui.out("ClassIsland 安装完成")
    return True


def list_available_versions(major: int | None = None) -> list[tuple[str, str]]:
    """汇总所有数据源的可用版本，返回去重并按版本降序排列的列表。

    :param major: 只保留指定大版本（``1`` / ``2``）；``None`` 表示不过滤。
    """
    from .disturb import _version_key

    collected: list[tuple[str, str]] = []
    seen: set[str] = set()

    for version, source_key in fetch_all_versions():
        if version not in seen:
            seen.add(version)
            collected.append((version, source_key))

    channels_info = pdc.fetch_distribution_channels()
    if channels_info:
        for channel_id, channel in (channels_info.get("channels") or {}).items():
            version = channel.get("latestVersion")
            if version and version not in seen:
                seen.add(version)
                collected.append((version, f"distribution:{channel_id}"))

    for release in github.fetch_releases(per_page=30):
        tag = release.get("tag_name")
        if not tag:
            continue
        version = github.version_from_tag(tag)
        if version and version not in seen and (
            version.startswith("1.") or version.startswith("2.")
        ):
            seen.add(version)
            collected.append((version, "github"))

    if major is not None:
        prefix = f"{major}."
        collected = [item for item in collected if item[0].startswith(prefix)]

    collected.sort(key=lambda item: _version_key(item[0]), reverse=True)
    return collected
