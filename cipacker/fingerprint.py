"""包指纹计算。

**为什么需要一个专门的指纹方案**

直觉做法是「算出 zip 文件的 SHA-256 写进包里」——但这在数学上不可能：
包内容一旦改变，其自身哈希随之改变，写入哈希又会再次改变内容，
形成自指悖论。

因此本模块区分两个概念：

``content_sha256`` （**包指纹**）
    基于**内容清单**计算，即把所有 ``(路径, 大小, 内容 SHA-256)`` 三元组
    排序后拼接再取哈希。它只描述「包里有哪些文件、内容是什么」，
    与压缩级别、时间戳、写入顺序**全部无关**。

    由此得到的关键性质：**同一份配置在任何机器、任何时间打包，
    指纹恒等**。这让「两份包是否等价」可以只比一个字符串就能判定，
    使包去重、缓存失效检测、往返一致性校验都成为可能。

``archive_sha256`` （**归档哈希**）
    zip 文件本身的字节哈希。仅在确定性模式下稳定，因此写进
    **旁挂文件** ``<包名>.sha256`` 而不是包内部，绕开自指问题。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .models import FileEntry, PackageFingerprint

_CHUNK_SIZE = 1024 * 256


def hash_file(path: Path) -> tuple[str, int]:
    """计算文件的 SHA-256 与字节数。

    :return: ``(hex_digest, size)``
    """
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_content_fingerprint(entries: list[FileEntry]) -> str:
    """基于内容清单计算包指纹。

    清单格式为逐行 ``path\\0size\\0sha256``，按路径字节序排序，
    分隔符选用 NUL 以避免路径中可能出现的歧义。
    """
    lines = []
    for entry in sorted(entries, key=lambda item: item.path):
        lines.append(f"{entry.path}\0{entry.size}\0{entry.sha256}")
    payload = "\n".join(lines).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_fingerprint(
    entries: list[FileEntry],
    archive_path: Path | None = None,
) -> PackageFingerprint:
    """计算完整指纹（内容指纹 + 可选的归档哈希）。"""
    archive_sha = None
    if archive_path is not None and archive_path.is_file():
        archive_sha, _ = hash_file(archive_path)

    return PackageFingerprint(
        content_sha256=compute_content_fingerprint(entries),
        archive_sha256=archive_sha,
        file_count=len(entries),
        total_bytes=sum(entry.size for entry in entries),
    )


def write_sidecar_checksum(archive_path: Path, digest: str) -> Path:
    """写出旁挂校验文件 ``<archive>.sha256``。

    格式与 ``sha256sum`` 工具兼容，便于用户用系统自带命令直接校验。
    """
    sidecar = archive_path.with_name(archive_path.name + ".sha256")
    sidecar.write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
    return sidecar


def read_sidecar_checksum(archive_path: Path) -> str | None:
    """读取旁挂校验文件中的哈希值。"""
    sidecar = archive_path.with_name(archive_path.name + ".sha256")
    if not sidecar.is_file():
        return None
    try:
        content = sidecar.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not content:
        return None
    return content.split()[0]


def fingerprints_match(left: PackageFingerprint, right: PackageFingerprint) -> bool:
    """两个指纹的内容部分是否一致。"""
    return left.content_sha256 == right.content_sha256
