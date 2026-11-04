"""Install optional on-demand speech without enabling automatic TTS."""
import shutil
import tempfile
from pathlib import Path

from .images import configure_send_timeout
from .runtime import AstrBot, Docker, wait_ready
from .state import UserError, read_json, write_json

PLUGIN = "astrbot_plugin_dsqq_voice"


def voice_command(inst, args, repo):
    path = inst.root / "astrbot/data/config" / (PLUGIN + "_config.json")
    config = read_json(path)
    backend = getattr(args, "backend", None) or config.get("backend", "edge")
    default_voice = "zh_female_vv_uranus_bigtts" if backend == "doubao" else "zh-CN-XiaoxiaoNeural"
    voice = args.voice or (config.get("voice") if config.get("backend", "edge") == backend else None) or default_voice
    credentials = None
    credential_target = inst.root / "astrbot/data/plugin_data" / PLUGIN / "doubao_credentials.json"
    if backend == "doubao":
        # Load only the standard-library adapter, without importing plugin dependencies.
        import importlib.util
        spec = importlib.util.spec_from_file_location("dsqq_doubao", repo / "plugins" / PLUGIN / "doubao.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            credentials = module.read_credentials(getattr(args, "credentials_file", None) or credential_target)
        except module.DoubaoError as exc:
            raise UserError(str(exc)) from None
    config.update({"enabled": True, "voice": voice, "backend": backend})
    config["resource_id"] = getattr(args, "resource_id", None) or config.get("resource_id", "seed-tts-2.0")
    if backend == "doubao":
        config["credentials_file"] = "/AstrBot/data/plugin_data/" + PLUGIN + "/doubao_credentials.json"
    docker = Docker(inst)
    docker.check()
    configure_send_timeout(inst, docker)
    target = inst.root / "astrbot/data/plugins" / PLUGIN
    target.parent.mkdir(parents=True, exist_ok=True)
    running = "astrbot" in docker.running()
    if running:
        docker.compose("stop", "astrbot")
    try:
        with tempfile.TemporaryDirectory(prefix="voice-install-", dir=inst.root) as temp:
            stage, backup = Path(temp) / "stage", Path(temp) / "backup"
            shutil.copytree(repo / "plugins" / PLUGIN, stage,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            if target.exists():
                target.rename(backup)
            try:
                stage.rename(target)
            except OSError:
                if backup.exists():
                    backup.rename(target)
                raise
        if credentials is not None:
            write_json(credential_target, credentials)
        write_json(path, config)
        path = inst.root / "astrbot/data/cmd_config.json"
        config = read_json(path)
        config.setdefault("provider_tts_settings", {})["enable"] = False
        write_json(path, config)
    finally:
        if running:
            docker.compose("start", "astrbot")
    if running:
        wait_ready(inst, 300)
        api = AstrBot(inst)
        if not any(t.get("name") == "send_voice" for t in api.call("GET", "/tools")):
            raise UserError("语音工具未加载，请查看 dsqq logs astrbot。")
        api.call("PATCH", "/tools/send_voice/permission", {"permission": "member"})
    print("按需语音已安装；群里 @机器人要求语音或使用 /语音 内容，平时保持文字回复。")
