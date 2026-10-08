"""迁移包的读取抽象与格式识别。

需要同时支持三种包：

``own-v3``
    本工具 v3 产出的包：含 ``__manifest__.json`` 且 ``format_version`` 为 3.x，
    路径**相对数据根**（不含 ``data/`` 前缀）。

``own-v2``（兼容旧版）
    旧版 CiPack.py 产出的包：含 ``__manifest__.json`` 且仅有 ``version`` 字段。
    旧版规则：CI 2.x 打包时**剥掉了** ``data/`` 前缀，因此路径同样相对数据根，
    可以直接沿用 own-v3 的解包逻辑。若检测到路径确实带 ``data/`` 前缀，
    则按带前缀处理（旧版在 v1 结构下会保留原样）。

``official``
    官方 ``.cidata`` / 裸 zip：不含清单，仅凭文件结构推断归属。
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .models import MANIFEST_NAME, Manifest


class ArchiveFormat(str, Enum):
    """包格式类型。"""

    OWN_V3 = "own-v3"
    OWN_V2 = "own-v2"
    OFFICIAL = "official"
    UNKNOWN = "unknown"

    def __str__(self) -> str:  # pragma: no cover
        return self.value


@dataclass
class ArchiveInfo:
    """迁移包的解析结果。"""

    path: Path
    format: ArchiveFormat = ArchiveFormat.UNKNOWN
    manifest: Manifest | None = None
    names: list[str] = field(default_factory=list)
    data_prefix: bool = False
    """包内路径是否带 ``data/`` 前缀。"""

    ci_structure: str = "unknown"
    ci_version: str | None = None

    @property
    def has_manifest(self) -> bool:
        return self.manifest is not None

    def file_names(self) -> list[str]:
        """去掉目录项后的文件路径列表。"""
        return [name for name in self.names if not name.endswith("/")]


def _has_data_prefix(names: list[str]) -> bool:
    """判断包内文件是否统一带 ``data/`` 前缀。"""
    files = [n for n in names if not n.endswith("/") and n != MANIFEST_NAME]
    if not files:
        return False
    data_files = [n for n in files if n.startswith("data/")]
    # 过半即认为带前缀，容忍个别例外
    return len(data_files) * 2 > len(files)


def _infer_official_structure(names: list[str]) -> tuple[str, str | None]:
    """从裸 zip 的文件结构推断 CI 结构与版本。"""
    files = [n for n in names if not n.endswith("/")]

    has_data_prefix = _has_data_prefix(names)
    has_settings = any(
        n in ("Settings.json", "data/Settings.json") for n in files
    )
    has_config = any(n.startswith("Config/") or n.startswith("data/Config/") for n in files)
    has_profiles = any(
        n.startswith("Profiles/") or n.startswith("data/Profiles/") for n in files
    )

    if has_data_prefix:
        return "v2", None

    if has_settings or (has_config and has_profiles):
        # 无法从结构确定版本，交由调用方用版本号判定
        return "unknown", None

    return "unknown", None


def read_archive(path: Path) -> ArchiveInfo:
    """读取并识别迁移包。

    :raises zipfile.BadZipFile: 文件不是有效 zip。
    """
    info = ArchiveInfo(path=path)

    with zipfile.ZipFile(path, "r") as archive:
        info.names = archive.namelist()

        if MANIFEST_NAME in info.names:
            raw = archive.read(MANIFEST_NAME).decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            info.manifest = Manifest.from_dict(payload)

            fmt_version = info.manifest.format_version or ""
            if fmt_version.startswith("3."):
                info.format = ArchiveFormat.OWN_V3
            else:
                info.format = ArchiveFormat.OWN_V2

            info.ci_structure = info.manifest.ci_structure or "unknown"
            info.ci_version = info.manifest.ci_version
            info.data_prefix = _has_data_prefix(info.names)
        else:
            info.format = ArchiveFormat.OFFICIAL
            inferred, version = _infer_official_structure(info.names)
            info.ci_structure = inferred
            info.ci_version = version
            info.data_prefix = _has_data_prefix(info.names)

    return info


def read_manifest(path: Path) -> Manifest | None:
    """仅读取包清单（不解压其余内容）。"""
    try:
        with zipfile.ZipFile(path, "r") as archive:
            if MANIFEST_NAME not in archive.namelist():
                return None
            raw = archive.read(MANIFEST_NAME).decode("utf-8", errors="replace")
            return Manifest.from_dict(json.loads(raw))
    except (zipfile.BadZipFile, json.JSONDecodeError, OSError):
        return None


# --------------------------------------------------------------------------- #
# 路径安全
# --------------------------------------------------------------------------- #


def is_safe_member(name: str, target_root: Path) -> tuple[bool, str]:
    """判断包内条目是否可以安全解压到目标目录（Zip Slip 防护）。

    :return: ``(safe, reason)``
    """
    if not name or name.endswith("/"):
        return True, ""

    normalized = name.replace("\\", "/")

    # 绝对路径
    if normalized.startswith("/"):
        return False, "绝对路径"

    # Windows 盘符，例如 C:/evil
    if len(normalized) >= 2 and normalized[1] == ":":
        return False, "带盘符的绝对路径"

    # 目录穿越
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if ".." in parts:
        return False, "路径穿越"

    # 空字节注入
    if "\x00" in name:
        return False, "空字节注入"

    # 最终路径必须落在目标目录内
    try:
        resolved = (target_root / normalized).resolve()
        root_resolved = target_root.resolve()
    except OSError:
        return False, "路径无法解析"

    if resolved != root_resolved and root_resolved not in resolved.parents:
        return False, "解析后越出目标目录"

    return True, ""


def normalize_member(name: str) -> str:
    """把包内路径规范化为「相对数据根」的形式。"""
    normalized = name.replace("\\", "/").lstrip("/")
    if normalized.startswith("data/"):
        return normalized[len("data/") :]
    return normalized
