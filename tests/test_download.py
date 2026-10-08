"""下载与传输层测试（P6）。

使用本地 :mod:`http.server` 模拟下载源，覆盖：

* 完整下载 + SHA512 校验通过
* 哈希不匹配被拒绝
* 非 ZIP 内容被拒绝
* 断点续传（Range 请求）
* 数据源注册表按大版本过滤
"""

from __future__ import annotations

import hashlib
import http.server
import socketserver
import threading
import unittest
import zipfile
from pathlib import Path

from cipacker.download import transfer
from cipacker.download.sources import SOURCE_REGISTRY, list_sources
from cipacker.errors import DownloadError

try:
    from .base import CIPackerTestCase
except ImportError:
    from base import CIPackerTestCase  # type: ignore


def make_zip_bytes(name: str = "a.txt", content: bytes = b"hello") -> bytes:
    """构造一个 zip。

    默认使用 zipfile.ZIP_STORED（不压缩），以便测试续传时
    zip 的实际字节数可预期（压缩会让小内容被压到几十字节）。
    """
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(name, content)
    return buf.getvalue()


class _Handler(http.server.BaseHTTPRequestHandler):
    """测试用 HTTP 处理器。

    注意：配置（payload / support_range）通过 :class:`_Server` 注入到
    *server 实例* 上，而非类属性，避免多个测试之间互相污染。
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静默
        pass

    @property
    def _payload(self) -> bytes:
        return self.server.payload  # type: ignore[attr-defined]

    @property
    def _support_range(self) -> bool:
        return self.server.support_range  # type: ignore[attr-defined]

    def do_HEAD(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(self._payload)))
        self.send_header("ETag", '"test-etag"')
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        body = self._payload
        rng = self.headers.get("Range")
        if rng and self._support_range:
            start_s, _, end_s = rng.replace("bytes=", "").partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else len(body) - 1
            if start >= len(body):
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            chunk = body[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)
            return

        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"test-etag"')
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)


class _Server(socketserver.TCPServer):
    """每个实例持有独立 payload，避免测试间串扰。"""

    allow_reuse_address = True

    def __init__(self, payload: bytes, support_range: bool = True):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.payload = payload
        self.support_range = support_range
        self.port = self.server_address[1]
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/pkg.zip"

    def stop(self):
        self.shutdown()
        self.server_close()


class TestTransfer(CIPackerTestCase):
    def _download(self, url: str, expected_sha512: str | None = None, target=None):
        save_dir = self.path("dl")
        save_dir.mkdir(exist_ok=True)
        if target is not None:
            # 预置半成品，触发续传：download_file 使用 <filename>.part
            (save_dir / "pkg.zip.part").write_bytes(Path(target).read_bytes())
        task = transfer.DownloadTask(
            url=url,
            filename="pkg.zip",
            sha512=expected_sha512,
        )
        result = transfer.download_file(task, save_dir, quiet=True)
        return result, result.path

    def test_successful_download(self):
        payload = make_zip_bytes("hi.txt", b"content")
        srv = _Server(payload)
        try:
            digest = hashlib.sha512(payload).hexdigest()
            _result, target = self._download(srv.url, digest)
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), payload)
        finally:
            srv.stop()

    def test_hash_mismatch_rejected(self):
        srv = _Server(make_zip_bytes())
        try:
            with self.assertRaises(DownloadError):
                self._download(srv.url, "0" * 128)
        finally:
            srv.stop()

    def test_non_zip_rejected(self):
        """服务器返回 HTML 错误页时应被 ZIP 魔数检查拦下。"""
        srv = _Server(b"<html>404 not found</html>")
        try:
            with self.assertRaises(DownloadError):
                self._download(srv.url)
        finally:
            srv.stop()

    def test_resume_from_partial(self):
        payload = make_zip_bytes("big.bin", b"x" * 40000)
        srv = _Server(payload)
        try:
            partial = self.path("partial.bin")
            partial.write_bytes(payload[:10000])
            digest = hashlib.sha512(payload).hexdigest()
            result, out = self._download(srv.url, digest, target=partial)

            self.assertEqual(out.read_bytes(), payload, "续传后内容应完整")
            self.assertGreater(result.resumed_from, 0, "应记录续传起点")
        finally:
            srv.stop()

    def test_probe_latency_returns_float(self):
        srv = _Server(make_zip_bytes())
        try:
            latency = transfer.probe_latency(srv.url, timeout=5)
            self.assertIsInstance(latency, float)
            self.assertGreaterEqual(latency, 0.0)
        finally:
            srv.stop()


class TestSourceRegistry(unittest.TestCase):
    def test_registry_not_empty(self):
        self.assertTrue(SOURCE_REGISTRY)

    def test_ci1x_sources_include_disturb(self):
        keys = {s.key for s in list_sources(1)}
        self.assertIn("disturb", keys)
        self.assertIn("github", keys)

    def test_ci2x_sources_include_distribution(self):
        keys = {s.key for s in list_sources(2)}
        self.assertIn("distribution", keys)

    def test_source_selection_respects_major(self):
        for source in list_sources(2):
            self.assertIn(source.applies_to, ("2.x", "any"))
        for source in list_sources(1):
            self.assertIn(source.applies_to, ("1.x", "any"))

    def test_ci1x_excludes_distribution(self):
        """官方分发中心只服务 2.x，不应出现在 1.x 列表。"""
        keys = {s.key for s in list_sources(1)}
        self.assertNotIn("distribution", keys)


class TestGithubHelpers(unittest.TestCase):
    def test_version_from_tag(self):
        from cipacker.download.github import version_from_tag

        self.assertEqual(version_from_tag("v2.1.0.1"), "2.1.0.1")
        self.assertEqual(version_from_tag("2.0.0"), "2.0.0")

    def test_asset_scoring_prefers_matching_platform(self):
        from cipacker.download.github import _score_asset

        win = _score_asset("ClassIsland-2.1.0.1-win-x64.zip", "win", "x64")
        mac = _score_asset("ClassIsland-2.1.0.1-osx-x64.zip", "win", "x64")
        self.assertGreater(win, mac)


if __name__ == "__main__":
    unittest.main()
