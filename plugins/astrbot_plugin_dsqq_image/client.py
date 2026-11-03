"""One bounded Images API request; never forward gateway credentials to image URLs."""
import base64
import binascii
import ipaddress
import json
from pathlib import Path
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_RESPONSE_BYTES = 30 * 1024 * 1024


class ImageError(Exception):
    pass


def read_bounded(response, limit):
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ImageError("生图结果超过大小限制。")
    return data


def public_image_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ImageError("图片下载地址必须是公开 HTTPS 地址。")
    try:
        addresses = socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ImageError("拒绝从本机或内网地址下载生成图片。")
    except (OSError, ValueError) as exc:
        raise ImageError("图片下载地址无法解析。") from exc
    return url


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, public_image_url(newurl))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A gateway redirect must not send its bearer token to another origin.
        raise ImageError("生图接口发生重定向，请检查网关 Base URL。")


def image_extension(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    raise ImageError("网关返回的内容不是 PNG、JPEG 或 WebP 图片。")


def save_response(payload, folder, timeout=60):
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise ImageError("生图接口没有返回图片。")
    item = rows[0]
    if item.get("b64_json"):
        try:
            encoded = item["b64_json"]
            if not isinstance(encoded, str) or len(encoded) > MAX_RESPONSE_BYTES:
                raise ImageError("生图结果超过大小限制。")
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ImageError("生图接口返回了无效的图片编码。") from exc
    elif item.get("url"):
        # Deliberately separate opener/request: no Authorization header on CDN requests.
        url = public_image_url(item["url"])
        opener = urllib.request.build_opener(PublicRedirect(), urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request(url), timeout=timeout) as response:
            data = read_bounded(response, MAX_IMAGE_BYTES)
    else:
        raise ImageError("生图接口没有返回图片 URL 或图片编码。")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError("生成图片超过 20 MB。")
    suffix = image_extension(data)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Only clean our generated files, retaining recent files for QQ media reads.
    for old in folder.glob("dsqq-image-*"):
        if old.is_file() and old.suffix in (".png", ".jpg", ".webp") and old.stat().st_mtime < time.time() - 7 * 86400:
            old.unlink()
    path = folder / ("dsqq-image-" + uuid.uuid4().hex + suffix)
    with path.open("xb") as stream:
        path.chmod(0o600)
        stream.write(data)
    return path


def generate_image(base_url, api_key, model, prompt, folder, timeout=300):
    prompt = prompt.strip()
    if not prompt or len(prompt) > 4000:
        raise ImageError("请提供 1–4000 字的图片描述。")
    body = {"model": model, "prompt": prompt, "n": 1, "size": "1024x1024"}
    req = urllib.request.Request(base_url.rstrip("/") + "/images/generations",
                                 data=json.dumps(body).encode(), headers={
                                     "Authorization": "Bearer " + api_key,
                                     "Content-Type": "application/json"})
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(req, timeout=timeout) as response:
            payload = json.loads(read_bounded(response, MAX_RESPONSE_BYTES))
        return save_response(payload, folder)
    except ImageError:
        raise
    except urllib.error.HTTPError as exc:
        raise ImageError(f"生图或图片下载接口返回 HTTP {exc.code}，请检查网关模型与额度。") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise ImageError("生图请求失败或超时；没有自动重试，请稍后查看是否已扣费再决定重试。") from exc
