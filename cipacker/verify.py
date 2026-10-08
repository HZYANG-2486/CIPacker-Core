"""迁移包完整性校验。

校验策略分两级：

**内容校验（默认）**
    重算包内每个文件的 SHA-256，与清单中的 ``files`` 清单比对，
    再重算内容指纹。可直接发现「文件被替换 / 截断 / 增删」。
    这是基于内容清单的校验，**不受自指悖论影响**。

**归档校验（``--deep``）**
    额外比对 zip 文件本身的字节哈希与旁挂 ``.sha256`` 文件，
    可发现「元数据被改动但内容未变」的情况。仅在确定性打包的包上有效。
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .archive import ArchiveFormat, read_archive
from .fingerprint import (
    compute_content_fingerprint,
    hash_file,
    read_sidecar_checksum,
)
from .models import MANIFEST_NAME, FileEntry, LayerName, PackageFingerprint


@dataclass
class VerifyIssue:
    """一条校验问题。"""

    kind: str  # missing | modified | added | fingerprint | archive | format
    path: str
    detail: str


@dataclass
class VerifyResult:
    """校验结果。"""

    path: Path
    ok: bool = True
    format: ArchiveFormat = ArchiveFormat.UNKNOWN
    checked_files: int = 0
    issues: list[VerifyIssue] = field(default_factory=list)
    expected_fingerprint: str = ""
    actual_fingerprint: str = ""
    archive_hash_ok: bool | None = None
    has_manifest: bool = True

    def add(self, kind: str, path: str, detail: str) -> None:
        self.issues.append(VerifyIssue(kind=kind, path=path, detail=detail))
        self.ok = False


def verify_archive(path: Path, *, deep: bool = False) -> VerifyResult:
    """校验迁移包完整性。

    :param path: 包路径。
    :param deep: 是否额外校验归档字节哈希（需旁挂 ``.sha256``）。
    """
    result = VerifyResult(path=path)

    info = read_archive(path)
    result.format = info.format

    if info.manifest is None:
        result.has_manifest = False
        # 无清单的官方包无法做内容校验，只能校验 zip 结构完整性
        try:
            with zipfile.ZipFile(path) as archive:
                bad = archive.testzip()
        except zipfile.BadZipFile as exc:
            result.add("format", str(path), f"不是有效的 zip: {exc}")
            return result

        if bad is not None:
            result.add("format", bad, "CRC 校验失败，文件已损坏")
            return result

        result.ok = True
        return result

    manifest = info.manifest

    # --- 归档字节哈希（可选） ------------------------------------------------- #
    if deep:
        sidecar = read_sidecar_checksum(path)
        expected_archive = (
            manifest.fingerprint.archive_sha256 if manifest.fingerprint else None
        ) or sidecar
        if expected_archive:
            actual_archive, _ = hash_file(path)
            result.archive_hash_ok = actual_archive == expected_archive
            if not result.archive_hash_ok:
                result.add(
                    "archive",
                    path.name,
                    f"归档哈希不匹配（期望 {expected_archive[:16]}…，实际 {actual_archive[:16]}…）",
                )

    # --- 逐文件内容校验 ------------------------------------------------------ #
    declared: dict[str, FileEntry] = {entry.path: entry for entry in manifest.files}

    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = set(archive.namelist())
            actual_entries: list[FileEntry] = []

            for name in sorted(names):
                if name == MANIFEST_NAME or name.endswith("/"):
                    continue

                try:
                    data = archive.read(name)
                except (zipfile.BadZipFile, KeyError) as exc:
                    result.add("format", name, f"读取失败: {exc}")
                    continue

                import hashlib

                digest = hashlib.sha256(data).hexdigest()
                size = len(data)
                result.checked_files += 1

                entry = declared.get(name)
                if entry is None:
                    result.add(
                        "added",
                        name,
                        "包内存在清单未声明的文件（包被改动过）",
                    )
                elif entry.sha256 != digest:
                    result.add(
                        "modified",
                        name,
                        f"内容哈希不匹配（期望 {entry.sha256[:16]}…，实际 {digest[:16]}…）",
                    )
                elif entry.size != size:
                    result.add(
                        "modified",
                        name,
                        f"大小不匹配（期望 {entry.size}，实际 {size}）",
                    )

                # 用清单声明的层，缺失时回退推断
                layer = entry.layer if entry else LayerName.SETTINGS
                actual_entries.append(
                    FileEntry(path=name, size=size, sha256=digest, layer=layer)
                )

            # 清单中声明但包内缺失
            for declared_path in declared:
                if declared_path not in names:
                    result.add("missing", declared_path, "清单声明但包内不存在")

            # --- 内容指纹 ---------------------------------------------------- #
            if manifest.fingerprint and manifest.fingerprint.content_sha256:
                result.expected_fingerprint = manifest.fingerprint.content_sha256
                result.actual_fingerprint = compute_content_fingerprint(actual_entries)
                if result.expected_fingerprint != result.actual_fingerprint:
                    result.add(
                        "fingerprint",
                        MANIFEST_NAME,
                        "内容指纹不匹配，包内容与清单不一致",
                    )
            else:
                result.actual_fingerprint = compute_content_fingerprint(actual_entries)

    except zipfile.BadZipFile as exc:
        result.add("format", str(path), f"不是有效的 zip: {exc}")

    return result


def verify_target_against_manifest(
    target_root: Path,
    manifest_fingerprint: PackageFingerprint,
    entries: list[FileEntry],
) -> list[str]:
    """校验某个已解包目录是否与清单一致（用于解包后自检）。"""
    problems: list[str] = []
    for entry in entries:
        candidate = target_root / entry.path
        if not candidate.is_file():
            problems.append(f"缺少文件: {entry.path}")
            continue
        digest, size = hash_file(candidate)
        if digest != entry.sha256:
            problems.append(f"内容不一致: {entry.path}")
        elif size != entry.size:
            problems.append(f"大小不一致: {entry.path}")
    return problems
