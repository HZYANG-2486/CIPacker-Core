"""下载源注册与测速选择。"""

from __future__ import annotations

from dataclasses import dataclass

from .transfer import probe_latency


@dataclass(frozen=True)
class DownloadSource:
    """一个可用于下载 ClassIsland 的数据源。"""

    key: str
    label: str
    applies_to: str  # "1.x" | "2.x" | "any"
    probe_url: str
    description: str = ""


#: 数据源注册表。
SOURCE_REGISTRY: tuple[DownloadSource, ...] = (
    DownloadSource(
        key="distribution",
        label="官方分发中心 (PDC)",
        applies_to="2.x",
        probe_url="https://distribution.classisland.tech/api/v1/public/distributions/web",
        description="官方分发 API，提供 SHA512 校验",
    ),
    DownloadSource(
        key="github",
        label="GitHub Releases",
        applies_to="any",
        probe_url="https://api.github.com/repos/ClassIsland/ClassIsland/releases/latest",
        description="覆盖全部历史版本",
    ),
    DownloadSource(
        key="disturb",
        label="Disturb 源",
        applies_to="1.x",
        probe_url="https://get.classisland.tech/d/ClassIsland-Ningbo-S3/classisland/disturb/index.json",
        description="CI 1.x 社区分发源",
    ),
    DownloadSource(
        key="disturb-net6",
        label="Disturb .NET 6 源",
        applies_to="1.x",
        probe_url="https://get.classisland.tech/d/ClassIsland-Ningbo-S3/classisland/disturb-net6/index.json",
        description="CI 1.x .NET 6 版本分发源",
    ),
)


def list_sources(major_version: int | None = None) -> list[DownloadSource]:
    """列出适用于指定主版本的数据源。

    :param major_version: ``1`` / ``2``；``None`` 表示全部。
    """
    if major_version is None:
        return list(SOURCE_REGISTRY)

    wanted = f"{major_version}.x"
    return [
        source
        for source in SOURCE_REGISTRY
        if source.applies_to in ("any", wanted)
    ]


def get_source(key: str) -> DownloadSource | None:
    for source in SOURCE_REGISTRY:
        if source.key == key:
            return source
    return None


@dataclass
class SourceProbe:
    """一次数据源测速结果。"""

    source: DownloadSource
    latency: float | None

    @property
    def reachable(self) -> bool:
        return self.latency is not None

    @property
    def display_latency(self) -> str:
        return f"{self.latency:.3f}s" if self.latency is not None else "不可达"


def ping_sources(
    sources: list[DownloadSource],
    *,
    timeout: float = 5.0,
) -> list[SourceProbe]:
    """并发度受控地测量各数据源延迟，返回按延迟升序排列的结果。"""
    probes = [
        SourceProbe(source=source, latency=probe_latency(source.probe_url, timeout))
        for source in sources
    ]
    probes.sort(key=lambda probe: (probe.latency is None, probe.latency or 0.0))
    return probes


def select_source(
    major_version: int | None = None,
    *,
    prefer: str | None = None,
    timeout: float = 5.0,
) -> SourceProbe | None:
    """选择最优数据源。

    :param prefer: 显式指定源 key，存在时直接返回（不做测速）。
    """
    if prefer:
        source = get_source(prefer)
        if source is None:
            return None
        return SourceProbe(source=source, latency=probe_latency(source.probe_url, timeout))

    probes = ping_sources(list_sources(major_version), timeout=timeout)
    for probe in probes:
        if probe.reachable:
            return probe
    return probes[0] if probes else None
