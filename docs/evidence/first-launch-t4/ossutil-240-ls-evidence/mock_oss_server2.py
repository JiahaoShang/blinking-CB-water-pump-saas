#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机 mock OSS 端点（v2）：为官方 ossutil 2.4.0 真二进制提供
ListObjectsV2（GET ?list-type=2）+ 对象 HEAD/GET/PUT 的最小子集，
覆盖 ls 预检的真实输出（非空/空/失败）与 cp 上传/下载/存在性语义。

仅监听 127.0.0.1；“云端”就是本机目录树（MOCK_CLOUD_ROOT/<bucket>/<key>），
不接触任何真实云服务。

环境：
  MOCK_PORT        监听端口（默认 18081）
  MOCK_CLOUD_ROOT  “云端”存储根目录
  MOCK_LOG         请求日志文件
  MOCK_MODE_FILE   行为开关文件（每次请求实时读取，便于同一服务切场景）：
                     fail-list -> 列表请求返回 403（OSS 风格错误 XML）
                     stale     -> 任何列表结果附加 <prefix>stale-precheck-object
"""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

PORT = int(os.environ.get("MOCK_PORT", "18081"))
LOG = os.environ.get("MOCK_LOG", "mock2-requests.log")
ROOT = os.path.abspath(os.environ.get("MOCK_CLOUD_ROOT", "mock-cloud"))


def esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def mode():
    try:
        with open(os.environ.get("MOCK_MODE_FILE", "/nonexistent-mode-file")) as fh:
            return fh.read().strip()
    except OSError:
        return ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _log(self, line):
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _respond(self, status, body=b"", ctype="application/xml", extra=None):
        data = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("x-oss-request-id", "mock-request-id")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _split(self):
        parsed = urlparse(self.path)
        raw = unquote(parsed.path).lstrip("/")
        bucket, _, key = raw.partition("/")
        return bucket, key, parse_qs(parsed.query)

    def _cloud_path(self, bucket, key):
        return os.path.join(ROOT, bucket, key)

    # ── HEAD：cp 的存在性探测 ─────────────────────────────────────────────
    def do_HEAD(self):
        bucket, key, _qs = self._split()
        target = self._cloud_path(bucket, key)
        if key and os.path.isfile(target):
            size = os.path.getsize(target)
            self._log("HEAD %s -> 200 (%d bytes)" % (self.path, size))
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.send_header("ETag", '"mock-etag"')
            self.send_header("x-oss-request-id", "mock-request-id")
            self.end_headers()
        else:
            self._log("HEAD %s -> 404" % self.path)
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    # ── GET：ls 列表 或 cp 下载 ──────────────────────────────────────────
    def do_GET(self):
        bucket, key, qs = self._split()
        if (qs.get("list-type") or [""])[0] == "2":
            self._do_list(bucket, (qs.get("prefix") or [""])[0], qs)
            return
        target = self._cloud_path(bucket, key)
        if key and os.path.isfile(target):
            with open(target, "rb") as fh:
                data = fh.read()
            self._log("GET %s -> 200 (%d bytes)" % (self.path, len(data)))
            self._respond(200, data, ctype="application/octet-stream")
        else:
            self._log("GET %s -> 404" % self.path)
            self._respond(404, (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message>"
                "<RequestId>mock-req</RequestId><HostId>mock-host</HostId></Error>"))

    def _do_list(self, bucket, prefix, qs):
        current = mode()
        self._log("LIST bucket=%s prefix=%r mode=%r" % (bucket, prefix, current))
        if current == "fail-list":
            self._respond(403, (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<Error><Code>AccessDenied</Code><Message>AccessDenied</Message>"
                "<RequestId>mock-req</RequestId><HostId>mock-host</HostId></Error>"))
            return
        keys = []
        base = os.path.join(ROOT, bucket, prefix)
        if os.path.isdir(base):
            for dirpath, _dirnames, filenames in os.walk(base):
                for name in sorted(filenames):
                    full = os.path.join(dirpath, name)
                    keys.append(os.path.relpath(full, os.path.join(ROOT, bucket)))
        if current == "stale":
            keys.append(prefix + "stale-precheck-object")
        contents = "".join(
            '<Contents><Key>%s</Key><LastModified>2026-10-13T11:08:29.000Z</LastModified>'
            '<ETag>"0123456789abcdef0123456789abcdef"</ETag><Type>Normal</Type>'
            "<Size>19186</Size><StorageClass>Standard</StorageClass></Contents>" % esc(k)
            for k in keys)
        enc = "<EncodingType>url</EncodingType>" if (qs.get("encoding-type") or [""])[0] else ""
        self._respond(200, (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<ListBucketResult><Name>%s</Name><Prefix>%s</Prefix>%s"
            "<MaxKeys>1000</MaxKeys><KeyCount>%d</KeyCount><IsTruncated>false</IsTruncated>%s"
            "</ListBucketResult>" % (esc(bucket), esc(prefix), enc, len(keys), contents)))

    # ── PUT：cp 上传（--ignore-existing 时可能带 x-oss-forbid-overwrite）──
    def do_PUT(self):
        bucket, key, _qs = self._split()
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else b""
        target = self._cloud_path(bucket, key)
        forbid = (self.headers.get("x-oss-forbid-overwrite") or "").lower() == "true"
        if forbid and os.path.isfile(target):
            self._log("PUT /%s -> 409 forbid-overwrite" % key)
            self._respond(409, (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<Error><Code>FileAlreadyExists</Code><Message>File already exists.</Message>"
                "<RequestId>mock-req</RequestId><HostId>mock-host</HostId></Error>"))
            return
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(data)
        self._log("PUT /%s -> 200 (%d bytes)" % (key, len(data)))
        self._respond(200, b"", extra={"ETag": '"mock-etag"'})

    def log_message(self, fmt, *args):  # 静音默认 stderr 日志
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
