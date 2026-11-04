"""Doubao Speech V3: one request, bounded NDJSON, no credential-bearing redirects."""
import base64
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid

ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
DEFAULT_VOICE = "zh_female_vv_uranus_bigtts"
MAX_RESPONSE = 16 * 1024 * 1024
MAX_LINE = 2 * 1024 * 1024


class DoubaoError(RuntimeError):
    pass


def read_credentials(path):
    if not path:
        raise DoubaoError("豆包语音凭证尚未配置。")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise DoubaoError("无法读取豆包语音凭证文件。") from None
    if not isinstance(data, dict):
        raise DoubaoError("豆包语音凭证文件必须是 JSON 对象。")
    def value(name):
        v = data.get(name, "")
        return str(v).strip() if isinstance(v, (str, int)) else ""
    result = {k: value(k) for k in ("api_key", "app_id", "access_token")}
    if not result["api_key"] and not (result["app_id"] and result["access_token"]):
        raise DoubaoError("请提供豆包语音 API Key，或 APP ID 和 Access Token。")
    if any("\r" in v or "\n" in v for v in result.values()):
        raise DoubaoError("豆包语音凭证格式不正确。")
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise DoubaoError("豆包语音接口发生重定向。")


def parse_audio(stream, timeout=60):
    audio = bytearray()
    total = 0
    deadline = time.monotonic() + timeout
    while True:
        line = stream.readline(MAX_LINE + 1)
        if not line:
            raise DoubaoError("豆包语音响应未完整结束。")
        total += len(line)
        if len(line) > MAX_LINE or total > MAX_RESPONSE:
            raise DoubaoError("豆包语音响应超过大小限制。")
        if time.monotonic() > deadline:
            raise DoubaoError("豆包语音请求超时。")
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            code = item.get("code")
            if code not in (0, 20000000):
                raise DoubaoError("豆包语音未能完成合成，请检查音色和服务权限。")
            if item.get("data"):
                audio.extend(base64.b64decode(item["data"], validate=True))
            if code == 20000000:
                if not audio:
                    raise DoubaoError("豆包语音返回了空音频。")
                return bytes(audio)
        except (ValueError, TypeError, AttributeError):
            raise DoubaoError("豆包语音返回了无效响应。") from None


def request_audio(text, voice, credentials_file, resource_id="seed-tts-2.0"):
    credentials = read_credentials(credentials_file)
    headers = {"Content-Type": "application/json", "X-Api-Resource-Id": resource_id,
               "X-Api-Request-Id": str(uuid.uuid4())}
    if credentials["api_key"]:
        headers["X-Api-Key"] = credentials["api_key"]
    else:
        headers["X-Api-App-Id"] = credentials["app_id"]
        headers["X-Api-Access-Key"] = credentials["access_token"]
    payload = {"user": {"uid": "dsqq"}, "req_params": {
        "text": text, "speaker": voice,
        "audio_params": {"format": "mp3", "sample_rate": 24000}}}
    request = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=45) as response:
            return parse_audio(response)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise DoubaoError("豆包语音请求失败，请检查凭证和服务状态。") from None
    except (urllib.error.URLError, OSError):
        # Neither raw responses nor credential-bearing request objects enter logs.
        raise DoubaoError("豆包语音请求失败，请检查凭证和服务状态。") from None
