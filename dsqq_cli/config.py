"""Render the container topology and initial AstrBot / NapCat configuration."""
import hashlib
import re
import secrets
import socket
from urllib.parse import urlsplit

from .state import UserError, write_json

ASTRBOT_IMAGE = "soulter/astrbot:v4.29.3"
NAPCAT_IMAGE = "mlikiowa/napcat-docker:v4.18.41"
SOURCE_ID = "dsqq_source"
MODEL_ID = "dsqq/chat"


def validate_model(base_url, model):
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UserError("API Base URL 必须是完整的 http(s) 地址。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise UserError("API Base URL 不能包含账号、密码、查询参数或 fragment。")
    if parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        raise UserError("模型在容器中调用；本机网关请使用 http://host.docker.internal:端口/v1。")
    if not model.strip() or any(ord(c) < 32 for c in model):
        raise UserError("请填写网关实际提供的模型名称。")


def free_port(preferred, excluded=()):
    for port in range(preferred, 65536):
        if port in excluded:
            continue
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                pass
    raise UserError("没有可用的本机端口。")


def new_config(owner, base_url, model, api_key, context, timezone):
    if not re.fullmatch(r"[0-9]{5,12}", owner):
        raise UserError("主人 QQ 号必须是 5–12 位数字。")
    validate_model(base_url, model)
    dashboard = free_port(6185)
    return {
        "kind": "dsqq-cli", "schema": 1,
        "project": "dsqq-" + secrets.token_hex(5),
        "docker_context": context,
        "timezone": timezone,
        "owner_qq": owner,
        "model": {"base_url": base_url.rstrip("/"), "name": model, "api_key": api_key},
        "images": {"astrbot": ASTRBOT_IMAGE, "napcat": NAPCAT_IMAGE},
        "ports": {"dashboard": dashboard, "napcat": free_port(6099, (dashboard,))},
        "secrets": {"dashboard_password": "Dfy1" + secrets.token_urlsafe(24),
                    "jwt_secret": secrets.token_hex(32),
                    "onebot_token": secrets.token_hex(24),
                    "napcat_token": secrets.token_hex(24)},
    }


def upsert(items, item):
    return [x for x in items if x.get("id") != item["id"]] + [item]


def render_model(conf, config):
    model = config["model"]
    conf["provider_sources"] = upsert(conf.get("provider_sources", []), {
        "id": SOURCE_ID, "provider": "openai", "type": "openai_chat_completion",
        "provider_type": "chat_completion", "enable": True, "key": [model["api_key"]],
        "api_base": model["base_url"], "timeout": 120, "proxy": "", "custom_headers": {},
    })
    old = next((p for p in conf.get("provider", []) if p.get("id") == MODEL_ID), {})
    if old.get("model") != model["name"]:
        old = {**old, "custom_extra_body": {}}
    conf["provider"] = upsert(conf.get("provider", []), {
        **old, "id": MODEL_ID, "provider_source_id": SOURCE_ID, "model": model["name"],
        "modalities": ["text", "image", "tool_use"], "enable": True,
    })
    runner = conf.setdefault("agent_runner", {"runner_type": "local", "config": {}})
    runner.setdefault("config", {}).setdefault("model", {})["provider_id"] = MODEL_ID
    conf.setdefault("provider_settings", {})["default_image_caption_provider_id"] = MODEL_ID
    return conf


def initial_astrbot(config):
    sec = config["secrets"]
    password = sec["dashboard_password"]
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    conf = {
        "config_version": 5,
        "admins_id": [config["owner_qq"]],
        "dashboard": {"enable": True, "host": "0.0.0.0", "port": 6185,
                      "username": "dafeiyu", "jwt_secret": sec["jwt_secret"],
                      "pbkdf2_password": f"pbkdf2_sha256$600000${salt}${digest}",
                      "password": hashlib.md5(password.encode()).hexdigest(),
                      "password_storage_upgraded": True, "password_change_required": False},
        "platform": [{"id": "dafeiyu", "type": "aiocqhttp", "enable": True,
                      "ws_reverse_host": "0.0.0.0", "ws_reverse_port": 6199,
                      "ws_reverse_token": sec["onebot_token"]}],
        "provider_settings": {"computer_use_runtime": "none"},
    }
    return render_model(conf, config)


def initial_napcat(config):
    return {"network": {"httpServers": [], "httpSseServers": [], "httpClients": [],
                        "websocketServers": [], "plugins": [], "websocketClients": [{
                            "name": "AstrBot", "enable": True, "url": "ws://astrbot:6199/ws",
                            "token": config["secrets"]["onebot_token"],
                            "messagePostFormat": "array", "reportSelfMessage": False,
                            "reconnectInterval": 1000, "heartInterval": 30000, "debug": False}]},
            "musicSignUrl": "", "enableLocalFile2Url": False, "parseMultMsg": False}


def compose_config(config):
    # Relative bind mounts keep projects movable. Share media at the SAME container path.
    def volume(source, target, read_only=False):
        return {"type": "bind", "source": source, "target": target, "read_only": read_only}

    common = {"restart": "unless-stopped", "logging": {"driver": "json-file", "options": {
        "max-size": "10m", "max-file": "3"}}}
    return {"services": {
        "astrbot": {**common, "image": config["images"]["astrbot"],
                    "environment": {"TZ": config["timezone"]},
                    "ports": [f"127.0.0.1:{config['ports']['dashboard']}:6185"],
                    "extra_hosts": ["host.docker.internal:host-gateway"],
                    "volumes": [volume("./astrbot/data", "/AstrBot/data"),
                                volume("./pack", "/dsqq/pack", True),
                                volume("./import", "/dsqq/import")]},
        "napcat": {**common, "image": config["images"]["napcat"],
                   "hostname": "dsqq-qq", "environment": {"TZ": config["timezone"]},
                   "ports": [f"127.0.0.1:{config['ports']['napcat']}:6099"],
                   "depends_on": ["astrbot"],
                   "volumes": [volume("./napcat/config", "/app/napcat/config"),
                               volume("./napcat/qq", "/app/.config/QQ"),
                               volume("./napcat/plugins", "/app/napcat/plugins"),
                               volume("./astrbot/data", "/AstrBot/data")]},
    }}


def initialize_files(inst):
    for directory in ("astrbot/data", "napcat/config", "napcat/qq", "napcat/plugins", "import"):
        (inst.root / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
    astrbot = inst.root / "astrbot/data/cmd_config.json"
    if not astrbot.exists():
        write_json(astrbot, initial_astrbot(inst.config))
    napcat = inst.root / "napcat/config"
    if not (napcat / "onebot11.json").exists():
        write_json(napcat / "onebot11.json", initial_napcat(inst.config))
    if not (napcat / "webui.json").exists():
        write_json(napcat / "webui.json", {
            "host": "0.0.0.0", "port": 6099, "token": inst.config["secrets"]["napcat_token"],
            "loginRate": 3,
        })
    write_json(inst.root / "compose.json", compose_config(inst.config))
