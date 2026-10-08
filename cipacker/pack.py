"""打包：把数据根内容写入迁移包。

**确定性打包**

默认启用。写入 zip 时做三件事，使「同一份输入 → 字节级相同的输出」：

1. 条目按 POSIX 路径**字节序排序**；
2. 每个条目使用**固定时间戳** ``1980-01-01 00:00:00``（ZIP 可表示的最早时间）、
   固定权限位与 ``create_system=0``；
3. 统一使用 ``ZIP_DEFLATED`` + ``compresslevel=9``。

关键在于**不使用** ``ZipFile.write()``——它会带上磁盘上的 mtime，
使输出随文件修改时间漂移，从而无法复现、无法比对。

``--preserve-mtime`` 可退回旧行为（保留真实时间戳），此时归档哈希不再稳定，
但内容指纹仍然稳定。
"""

from __future__ import annotations

import platform
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .discovery import DiscoveredFile
from .fingerprint import compute_content_fingerprint, write_sidecar_checksum
from .layers import LayerSelection, assign_layer
from .layout import Layout, detect_ci_version
from .models import (
    DETERMINISTIC_TIMESTAMP,
    FORMAT_VERSION,
    MANIFEST_NAME,
    FileEntry,
    LayerName,
    Manifest,
    PackageFingerprint,
)
from .plugins import scan_plugins

#: 流式拷贝的块大小（保持内存占用恒定，与文件体积无关）
CHUNK_SIZE = 1024 * 1024

#: 确定性模式下使用的固定权限位。
FIXED_MODE = 0o644

#: 压缩级别。
COMPRESS_LEVEL = 9


@dataclass
class PackItem:
    """待写入包的一个文件。"""

    rel_posix: str
    source: Path
    size: int
    layer: LayerName
    mode: int = FIXED_MODE


@dataclass
class PackResult:
    """打包结果。"""

    output: Path
    manifest: Manifest
    fingerprint: PackageFingerprint
    sidecar: Path | None = None
    entries: list[FileEntry] = field(default_factory=list)
    skipped_layers: list[LayerName] = field(default_factory=list)

    @property
    def archive_size(self) -> int:
        return self.output.stat().st_size if self.output.exists() else 0


# --------------------------------------------------------------------------- #
# 收集
# --------------------------------------------------------------------------- #


def collect_pack_items(
    layout: Layout,
    selection: LayerSelection,
    discovered: list[DiscoveredFile],
    *,
    redact: bool = False,
) -> tuple[list[PackItem], list[str], list[str]]:
    """把发现的文件按层筛选为待打包项。

    :return: ``(items, redacted_fields, warnings)``
    """
    items: list[PackItem] = []
    redacted: list[str] = []
    warnings: list[str] = []

    app_root = layout.app_root

    for discovered_file in discovered:
        layer = assign_layer(discovered_file.rel)
        if not selection.includes(layer):
            continue

        source = app_root / discovered_file.rel
        if not source.is_file():
            warnings.append(f"文件在打包过程中消失，已跳过: {discovered_file.rel}")
            continue

        items.append(
            PackItem(
                rel_posix=discovered_file.rel,
                source=source,
                size=discovered_file.size,
                layer=layer,
            )
        )

    items.sort(key=lambda item: item.rel_posix)

    if redact:
        redacted = [item.rel_posix for item in items]

    return items, redacted, warnings


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #


def _build_zip_info(name: str, *, deterministic: bool, source: Path | None = None) -> zipfile.ZipInfo:
    """构造一个用于写入的 :class:`zipfile.ZipInfo`。"""
    if deterministic:
        info = zipfile.ZipInfo(filename=name, date_time=DETERMINISTIC_TIMESTAMP)
        info.external_attr = FIXED_MODE << 16
        info.create_system = 0  # 0 = MS-DOS，跨平台一致
    else:
        info = zipfile.ZipInfo(filename=name)
        if source is not None:
            try:
                stat = source.stat()
                info.date_time = _mtime_to_tuple(stat.st_mtime)
                info.external_attr = (stat.st_mode & 0xFFFF) << 16
            except OSError:
                info.date_time = DETERMINISTIC_TIMESTAMP
        info.create_system = 0
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _mtime_to_tuple(timestamp: float) -> tuple[int, int, int, int, int, int]:
    import time as _time

    parts = _time.localtime(timestamp)
    year = max(1980, parts.tm_year)
    return (year, parts.tm_mon, parts.tm_mday, parts.tm_hour, parts.tm_min, parts.tm_sec)


def _sha256_of(path: Path) -> tuple[str, int]:
    import hashlib

    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 256)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def write_package(
    items: list[PackItem],
    output: Path,
    manifest: Manifest,
    *,
    deterministic: bool = True,
    include_manifest: bool = True,
    progress_callback: object | None = None,
) -> tuple[list[FileEntry], PackageFingerprint, Path | None]:
    """把待打包项写入 zip 并计算指纹。

    :return: ``(entries, fingerprint, sidecar_path)``
    """
    entries: list[FileEntry] = []

    # 先算逐文件哈希（确定性模式下这个顺序不影响结果，因为最后会排序）
    for item in items:
        sha256, size = _sha256_of(item.source)
        entries.append(
            FileEntry(
                path=item.rel_posix,
                size=size,
                sha256=sha256,
                layer=item.layer,
                mode=item.mode,
            )
        )

    content_fp = compute_content_fingerprint(entries)
    fingerprint = PackageFingerprint(
        content_sha256=content_fp,
        archive_sha256=None,
        file_count=len(entries),
        total_bytes=sum(entry.size for entry in entries),
    )

    manifest.files = entries
    manifest.file_count = len(entries)
    manifest.total_bytes = fingerprint.total_bytes
    manifest.fingerprint = fingerprint
    manifest.deterministic = deterministic

    output.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=COMPRESS_LEVEL,
    ) as archive:
        # 清单固定放在第一个条目：便于流式读取时先拿到元信息
        if include_manifest:
            payload = manifest.to_json().encode("utf-8")
            info = _build_zip_info(MANIFEST_NAME, deterministic=deterministic)
            archive.writestr(info, payload)

        for item in items:
            info = _build_zip_info(
                item.rel_posix,
                deterministic=deterministic,
                source=item.source if not deterministic else None,
            )
            # 流式写入，避免把大文件（插件 DLL 动辄数十 MB）整个读进内存
            with open(item.source, "rb") as handle:
                with archive.open(info, "w") as target:
                    shutil.copyfileobj(handle, target, CHUNK_SIZE)

            if callable(progress_callback):
                progress_callback(item.rel_posix)

    # 归档哈希：确定性模式下稳定，写入旁挂文件
    archive_sha = None
    sidecar: Path | None = None
    if deterministic:
        from .fingerprint import hash_file

        archive_sha, _ = hash_file(output)
        fingerprint = PackageFingerprint(
            content_sha256=content_fp,
            archive_sha256=archive_sha,
            file_count=len(entries),
            total_bytes=fingerprint.total_bytes,
        )
        manifest.fingerprint = fingerprint
        sidecar = write_sidecar_checksum(output, archive_sha)

    return entries, fingerprint, sidecar


# --------------------------------------------------------------------------- #
# 高层入口
# --------------------------------------------------------------------------- #


def build_manifest(
    layout: Layout,
    selection: LayerSelection,
    *,
    include_backups: bool,
    deterministic: bool,
    redacted: list[str] | None = None,
) -> Manifest:
    """构造包清单（不含文件列表，由 :func:`write_package` 填充）。"""
    from .models import utc_now_iso

    return Manifest(
        format_version=FORMAT_VERSION,
        tool="CIPacker",
        tool_version=__version__,
        created_at=utc_now_iso(),
        ci_structure=layout.kind.value,
        ci_version=detect_ci_version(layout.app_root),
        source_platform=f"{platform.system().lower()}_{platform.machine().lower()}",
        layers=list(selection.selected),
        include_backups=include_backups,
        deterministic=deterministic,
        redacted=list(redacted or []),
        plugins=scan_plugins(layout, quiet=True),
    )


def deterministic_manifest(manifest: Manifest, timestamp: str = "") -> Manifest:
    """把清单中随环境变化的字段置为稳定值，使包内容完全可复现。

    用于「确定性」承诺：``created_at``、``source_platform`` 等每次运行
    都会变化，若不做处理，两份「相同配置」的包内容仍会不同。
    """
    manifest.created_at = timestamp or "1970-01-01T00:00:00+00:00"
    manifest.source_platform = ""
    return manifest
