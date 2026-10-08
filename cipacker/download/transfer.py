"""传输层：断点续传、重试、完整性校验与 ZIP 头验证。

旧版把这段逻辑写在 ``download_ci_package`` 里，与业务逻辑耦合，
导致无法单独测试。这里抽成纯粹的函数，进度与错误通过回调上报。
"""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import logui
from ..errors import DownloadError

try:  # pragma: no cover - 可选依赖
    from tqdm import tqdm

    TQDM_AVAILABLE = True
except ImportError:  # pragma: no cover
    tqdm = None
    TQDM_AVAILABLE = False

CHUNK_SIZE = 256 * 1024

#: ZIP 文件头（本地文件头签名）。
ZIP_MAGIC = b"PK\x03\x04"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_UA = "CIPacker/3.0"


@dataclass
class DownloadTask:
    """一次待执行的下载。"""

    url: str
    filename: str
    size: int = 0
    sha512: str | None = None
    sha256: str | None = None
    #: 需要校验 ZIP 头（ClassIsland 发行包恒为 zip）。
    expect_zip: bool = True


@dataclass
class DownloadResult:
    """下载结果。"""

    path: Path
    size: int
    resumed_from: int = 0
    verified: bool = False
    warnings: list[str] = field(default_factory=list)


ProgressCallback = Callable[[float, int, int], None]


def build_headers(*, spoof_ua: bool, offset: int = 0) -> dict[str, str]:
    """构造请求头，含断点续传的 Range。"""
    headers = {
        "User-Agent": BROWSER_UA if spoof_ua else DEFAULT_UA,
        "Accept": "application/octet-stream,*/*",
    }
    if spoof_ua:
        headers["Referer"] = "https://www.classisland.tech/"
    if offset > 0:
        headers["Range"] = f"bytes={offset}-"
    return headers


def hash_file(path: Path, algorithm: str = "sha512") -> str:
    """计算文件哈希。"""
    digest = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def download_file(
    task: DownloadTask,
    save_dir: Path,
    *,
    spoof_ua: bool = False,
    timeout: float = 30.0,
    retries: int = 3,
    progress: ProgressCallback | None = None,
    quiet: bool = False,
) -> DownloadResult:
    """下载文件，支持断点续传与重试。

    :raises DownloadError: 重试耗尽或校验失败。
    """
    save_dir.mkdir(parents=True, exist_ok=True)
    part_path = save_dir / f"{task.filename}.part"
    final_path = save_dir / task.filename

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            result = _attempt_download(
                task,
                part_path,
                final_path,
                spoof_ua=spoof_ua,
                timeout=timeout,
                progress=progress,
                quiet=quiet,
            )
            if result is not None:
                return result
        except KeyboardInterrupt:
            if part_path.exists():
                logui.out(f"\n下载已中断，保留部分文件以便续传: {part_path.name}")
            raise
        except Exception as exc:  # noqa: BLE001 - 需要重试所有网络异常
            last_error = exc
            if attempt < retries:
                if not quiet:
                    logui.warn(f"下载失败（第 {attempt}/{retries} 次）: {exc}")
                time.sleep(min(2 ** attempt, 8))
                continue

    raise DownloadError(
        f"下载失败: {last_error}",
        hint="请检查网络连接，或改用 --source 指定其它下载源",
    )


def _attempt_download(
    task: DownloadTask,
    part_path: Path,
    final_path: Path,
    *,
    spoof_ua: bool,
    timeout: float,
    progress: ProgressCallback | None,
    quiet: bool,
) -> DownloadResult | None:
    """执行一次下载尝试。返回 None 表示需要重试。"""
    existing = part_path.stat().st_size if part_path.exists() else 0
    resume_offset = existing  # 记录进入本次尝试时的续传起点
    headers = build_headers(spoof_ua=spoof_ua, offset=existing)

    request = urllib.request.Request(task.url, headers=headers)

    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and existing > 0:
            # 已下载部分比服务端文件还大，说明 .part 是脏数据
            part_path.unlink(missing_ok=True)
            raise DownloadError("续传位置异常，已重置") from exc
        raise

    with response:
        status = response.status
        resumed = status == 206

        # 服务器不支持续传时，从头开始
        if existing > 0 and not resumed:
            existing = 0
            part_path.unlink(missing_ok=True)

        content_length = response.headers.get("Content-Length")
        total = task.size
        if content_length:
            declared = int(content_length)
            total = existing + declared if resumed else declared

        mode = "ab" if resumed and existing > 0 else "wb"

        if not quiet:
            if existing > 0 and resumed:
                logui.out(
                    f"断点续传: 已下载 {logui.human_size(existing)} / "
                    f"{logui.human_size(total) if total else '未知'}"
                )
            else:
                logui.out(
                    f"开始下载: {task.filename} "
                    f"({logui.human_size(total) if total else '大小未知'})"
                )

        downloaded = existing if mode == "ab" else 0
        first_chunk = b""

        with open(part_path, mode) as handle:
            # 首次下载时校验文件头，尽早发现服务端返回错误页
            if mode == "wb" and task.expect_zip:
                first_chunk = response.read(len(ZIP_MAGIC))
                if len(first_chunk) < len(ZIP_MAGIC) or first_chunk != ZIP_MAGIC:
                    part_path.unlink(missing_ok=True)
                    raise DownloadError(
                        f"下载内容不是有效的 ZIP（文件头: {first_chunk.hex() or '空'}）",
                        hint="可能是服务端错误页，请改用其它下载源",
                    )
                handle.write(first_chunk)
                downloaded += len(first_chunk)

            bar = _make_progress_bar(total, task.filename, quiet)
            if bar is not None and downloaded > 0:
                bar.update(downloaded)

            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                if bar is not None:
                    bar.update(len(chunk))
                if progress is not None and total > 0:
                    progress(min(100.0, downloaded * 100.0 / total), downloaded, total)

            if bar is not None:
                bar.close()

    if final_path.exists():
        final_path.unlink()
    part_path.replace(final_path)

    result = DownloadResult(
        path=final_path,
        size=final_path.stat().st_size,
        resumed_from=resume_offset if resumed else 0,
    )

    # 完整性校验
    if task.sha512:
        if not quiet:
            logui.out("正在校验文件完整性 (SHA512)…")
        actual = hash_file(final_path, "sha512")
        if actual.lower() == task.sha512.lower():
            result.verified = True
        else:
            final_path.unlink(missing_ok=True)
            raise DownloadError(
                f"SHA512 校验失败\n  期望: {task.sha512}\n  实际: {actual}"
            )
    elif task.sha256:
        actual = hash_file(final_path, "sha256")
        if actual.lower() == task.sha256.lower():
            result.verified = True
        else:
            final_path.unlink(missing_ok=True)
            raise DownloadError(f"SHA256 校验失败\n  实际: {actual}")
    else:
        result.warnings.append("该数据源未提供校验值，已跳过完整性校验")

    return result


def _make_progress_bar(total: int, desc: str, quiet: bool):
    if quiet or not TQDM_AVAILABLE or tqdm is None:
        return None
    return tqdm(
        total=total if total > 0 else None,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc=desc[:40],
        leave=False,
    )


def probe_latency(url: str, timeout: float = 5.0) -> float | None:
    """测量 URL 响应延迟（秒）；不可达返回 None。"""
    try:
        request = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": BROWSER_UA}
        )
        start = time.time()
        with urllib.request.urlopen(request, timeout=timeout):
            pass
        return time.time() - start
    except Exception:  # noqa: BLE001 - 探测失败即视为不可达
        return None


def fetch_json(url: str, *, timeout: float = 15.0, headers: dict | None = None) -> object | None:
    """抓取 JSON；失败返回 None（不抛异常，便于多源回退）。"""
    request_headers = {"User-Agent": DEFAULT_UA, "Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    for attempt_headers in (request_headers, {**request_headers, "User-Agent": BROWSER_UA}):
        try:
            request = urllib.request.Request(url, headers=attempt_headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
            if raw.lstrip().startswith("<"):
                # 返回了 HTML（通常是分发站点的目录页）
                continue
            import json

            return json.loads(raw)
        except Exception:  # noqa: BLE001
            continue

    return None
