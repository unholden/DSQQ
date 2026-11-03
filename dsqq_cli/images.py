"""Install the bundled image tool, preserving the existing model and QQ session."""
import json
import shutil
import tempfile
from pathlib import Path

from .runtime import AstrBot, Docker, NapCat, wait_ready
from .state import UserError, read_json, write_json

PLUGIN = "astrbot_plugin_dsqq_image"


def tune_send_timeout(config):
    """Raise only the send baseline; preserve user settings above our floor."""
    timeout = config.setdefault("timeout", {})
    timeout["baseTimeout"] = max(30000, int(timeout.get("baseTimeout", 10000)))
    return config


def configure_send_timeout(inst, docker):
    # Online account config is owned by NapCat: update through its supported API.
    if "napcat" in docker.running():
        api = NapCat(inst)
        config = api.call("/OB11Config/GetConfig")
        old = config.get("timeout", {}).get("baseTimeout")
        tune_send_timeout(config)
        if old != config["timeout"]["baseTimeout"]:
            api.call("/OB11Config/SetConfig", {"config": json.dumps(config)})
        current = api.call("/OB11Config/GetConfig")
        if current.get("timeout", {}).get("baseTimeout", 0) < 30000:
            raise UserError("NapCat 发送等待时间未生效，请检查配置。")
    else:
        for path in (inst.root / "napcat/config").glob("onebot11*.json"):
            write_json(path, tune_send_timeout(read_json(path)))


def image_command(inst, args, repo):
    docker = Docker(inst)
    docker.check()
    if args.action == "install":
        configure_send_timeout(inst, docker)
        source = repo / "plugins" / PLUGIN
        target = inst.root / "astrbot/data/plugins" / PLUGIN
        target.parent.mkdir(parents=True, exist_ok=True)
        running = "astrbot" in docker.running()
        if running:
            docker.compose("stop", "astrbot")
        try:
            with tempfile.TemporaryDirectory(prefix="image-install-", dir=inst.root) as temp:
                temp = Path(temp)
                stage, backup = temp / "stage", temp / "backup"
                shutil.copytree(source, stage, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                if target.exists():
                    target.rename(backup)
                try:
                    stage.rename(target)
                except OSError:
                    if backup.exists():
                        backup.rename(target)
                    raise
            path = inst.root / "astrbot/data/config" / (PLUGIN + "_config.json")
            config = read_json(path)
            config.update({"model": args.model, "provider_source_id": "dsqq_source"})
            config.setdefault("enabled", True)
            config.setdefault("timeout_seconds", 300)
            write_json(path, config)
        finally:
            if running:
                docker.compose("start", "astrbot")
        if running:
            wait_ready(inst, 300)
            api = AstrBot(inst)
            if not any(t.get("name") == "generate_image" for t in api.call("GET", "/tools")):
                raise UserError("生图工具未加载，请查看 dsqq logs astrbot。")
            api.call("PATCH", "/tools/generate_image/permission", {"permission": "admin"})
        print("生图插件已安装（仅管理员）；QQ 可发 /生图 图片描述，或自然语言要求画图。")
    else:
        # The secret stays inside the mounted config; do not put it in argv or stdout.
        script = '''import sys,json
from pathlib import Path
sys.path.insert(0,"/AstrBot/data/plugins/astrbot_plugin_dsqq_image")
from client import generate_image
root=Path("/AstrBot/data")
c=json.loads((root/"cmd_config.json").read_text(encoding="utf-8-sig"))
p=json.loads((root/"config/astrbot_plugin_dsqq_image_config.json").read_text(encoding="utf-8-sig"))
s=next(v for v in c["provider_sources"] if v["id"]==p["provider_source_id"])
key=s["key"][0] if isinstance(s["key"],list) else s["key"]
path=generate_image(s["api_base"],key,p["model"],sys.argv[1],root/"plugin_data/astrbot_plugin_dsqq_image",p.get("timeout_seconds",300))
print(path)
'''
        print("生成测试图片（会调用模型）；仅保存文件，不发送 QQ 消息…", flush=True)
        output = docker.compose("exec", "-T", "astrbot", "python", "-c", script, args.prompt, timeout=400)
        print(output.strip())
