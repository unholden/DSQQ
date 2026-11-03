"""Terminal-only workflows; never opens a browser or a native application."""
import argparse
import copy
import getpass
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from urllib.parse import quote, urlencode

from .config import initialize_files, new_config, render_model, validate_model, MODEL_ID
from .runtime import AstrBot, Docker, NapCat, docker_context, run, wait_ready
from .state import Installation, UserError, private_write, read_json, write_json

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path.home() / "Library/Application Support/DSQQ"


def dump(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def credential(args, existing=""):
    if args.api_key_file:
        value = Path(args.api_key_file).expanduser().read_text(encoding="utf-8").strip()
    elif args.api_key_env:
        value = os.environ.get(args.api_key_env, "").strip()
    elif existing:
        return existing
    elif not args.non_interactive and sys.stdin.isatty():
        value = getpass.getpass("API Key（New API 签发的令牌，输入不显示）：").strip()
    else:
        raise UserError("请用 --api-key-env 或 --api-key-file 提供 API Key。")
    if not value:
        raise UserError("API Key 不能为空。")
    return value


def prompt(value, label, default, non_interactive):
    if value is not None:
        return value
    if non_interactive or not sys.stdin.isatty():
        if default is None:
            raise UserError(f"缺少参数：{label}")
        return default
    return input(f"{label}" + (f" [{default}]" if default else "") + "：").strip() or default or ""


def init(inst, args):
    with inst.lock(creating=True):
        if inst.path.exists():
            raise UserError("已初始化；使用 configure 修改模型，up 继续启动。")
        owner = prompt(args.owner_qq, "主人 QQ 号", None, args.non_interactive)
        base = prompt(args.base_url, "API Base URL", None, args.non_interactive)
        model = prompt(args.model, "模型名称", None, args.non_interactive)
        config = new_config(owner, base, model, credential(args), docker_context(args.context), args.timezone)
        if args.astrbot_image:
            config["images"]["astrbot"] = args.astrbot_image
        if args.napcat_image:
            config["images"]["napcat"] = args.napcat_image
        inst.config = config
        os.chmod(inst.root, 0o700)
        inst.save()  # Persist random credentials BEFORE rendering; interrupted init is recoverable via up.
        ensure_files(inst)
        print(f"配置已保存：{inst.root}\n下一步：dsqq install（安装服务与配置包），然后 dsqq login 扫码。")


def ensure_files(inst):
    initialize_files(inst)
    pack = inst.root / "pack"
    if not pack.exists():
        # Stage + rename prevents an interrupted copy from looking complete.
        with tempfile.TemporaryDirectory(prefix=".pack-", dir=inst.root) as temp:
            staged = Path(temp) / "pack"
            shutil.copytree(REPO / "vendor/dafeiyu-config-pack", staged,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            staged.rename(pack)


def up(inst, args, pull=False):
    ensure_files(inst)
    docker = Docker(inst)
    docker.check()
    if pull:
        print("拉取固定版本镜像（首次下载可能需要几分钟）…", flush=True)
        docker.stream("pull")
    print("启动 AstrBot / NapCat…", flush=True)
    docker.compose("up", "-d", timeout=900)
    print("等待本机管理接口认证成功…", flush=True)
    wait_ready(inst, args.timeout)
    print("服务已就绪；用 dsqq login 在终端扫码。")


def configure(inst, args):
    new = copy.deepcopy(inst.config)
    model = new["model"]
    model["base_url"] = (args.base_url or model["base_url"]).rstrip("/")
    model["name"] = args.model or model["name"]
    model["api_key"] = credential(args, model["api_key"])
    validate_model(model["base_url"], model["name"])
    docker = Docker(inst)
    docker.check()
    was_running = "astrbot" in docker.running()
    if was_running:
        docker.compose("stop", "astrbot")
    path = inst.root / "astrbot/data/cmd_config.json"
    try:
        conf = read_json(path)
        write_json(path, render_model(conf, new))
        inst.config = new
        inst.save()
    finally:
        if was_running:
            docker.compose("start", "astrbot")
    print("模型配置已更新；没有重置人格、插件或 MCP。")


def status(inst, args):
    docker = Docker(inst)
    docker.check()
    processes = [{k: row.get(k) for k in ("Service", "State", "Health", "ExitCode")}
                 for row in docker.processes()]
    result = {"root": str(inst.root), "docker_context": inst.config["docker_context"],
              "services": processes, "astrbot_api": False, "qq": "unavailable"}
    try:
        AstrBot(inst).login()
        result["astrbot_api"] = True
    except UserError:
        pass
    try:
        qq = NapCat(inst).call("/QQLogin/CheckLoginStatus")
        result["qq"] = "online" if qq.get("isLogin") else (
            "offline" if qq.get("isOffline") else "need_scan" if qq.get("qrcodeurl") else "starting")
    except UserError:
        pass
    if args.json:
        dump(result)
    else:
        for row in processes:
            print(f"{row['Service']}: {row['State']}")
        print(f"AstrBot 管理 API: {'ready' if result['astrbot_api'] else 'unavailable'}")
        print(f"QQ: {result['qq']}（QQ 在线不代表模型 API 已验证；可运行 test-model）")
    return 0 if result["astrbot_api"] and result["qq"] == "online" else 1


def login(inst, args):
    api = NapCat(inst)
    if args.refresh:
        api.call("/QQLogin/RefreshQRcode")
    deadline = time.monotonic() + args.timeout
    last_url = None
    print("等待 QQ 登录，请用手机 QQ 扫描终端二维码…", flush=True)
    while time.monotonic() < deadline:
        data = api.call("/QQLogin/CheckLoginStatus")
        if data.get("isLogin"):
            info = api.call("/QQLogin/GetQQLoginInfo")
            print(f"QQ 已登录：{info.get('uin', '')}")
            return
        if data.get("loginError"):
            raise UserError(str(data["loginError"]) + "；可运行 login --refresh 换码。")
        url = data.get("qrcodeurl")
        if url and url != last_url:
            if shutil.which("qrencode"):
                rendered = run(["qrencode", "-t", "ANSIUTF8"], input_text=url)
                if rendered.returncode:
                    raise UserError("终端二维码生成失败。")
                print(rendered.stdout, flush=True)
            else:
                # qrcode is installed in AstrBot's runtime; no host pip dependency needed.
                script = "import sys,qrcode; q=qrcode.QRCode(border=2); q.add_data(sys.stdin.read().strip()); q.print_ascii(invert=True)"
                docker = Docker(inst)
                rendered = run(docker.compose_prefix + ["exec", "-T", "astrbot", "python", "-c", script], input_text=url)
                if rendered.returncode:
                    raise UserError("缺少二维码生成器；运行 brew install qrencode 后重试。")
                print(rendered.stdout, flush=True)
            last_url = url
        time.sleep(2)
    raise UserError("扫码等待超时；服务仍在后台运行，可再次运行 login。")


def import_pack(inst, args):
    docker = Docker(inst)
    docker.check()
    AstrBot(inst).login()
    ensure_files(inst)
    answers = {"base_url": "http://127.0.0.1:6185", "username": "dafeiyu",
               "owner_qq": inst.config["owner_qq"], "memory": "2" if args.memory == "siliconflow" else "0",
               "voice": "3" if args.voice == "doubao" else "0"}
    secret_values = {"password": inst.config["secrets"]["dashboard_password"]}
    if args.memory == "siliconflow":
        secret_values["siliconflow_key"] = os.environ.get("SILICONFLOW_API_KEY", "")
    if args.voice == "doubao":
        answers["volc_app_id"] = os.environ.get("DOUBAO_APP_ID", "")
        secret_values["volc_access_token"] = os.environ.get("DOUBAO_ACCESS_TOKEN", "")
        if not answers["volc_app_id"]:
            raise UserError("请设置 DOUBAO_APP_ID。")
    if not all(secret_values.values()):
        raise UserError("请设置所选功能需要的 SILICONFLOW_API_KEY / DOUBAO_ACCESS_TOKEN。")
    # A single private directory is removed even when import fails or is interrupted.
    with tempfile.TemporaryDirectory(prefix="job-", dir=inst.root / "import") as temp:
        folder = Path(temp)
        internal = "/dsqq/import/" + folder.name
        for name, value in secret_values.items():
            private_write(folder / f"{name}.txt", value)
            answers[name + "_file"] = internal + f"/{name}.txt"
        write_json(folder / "answers.json", answers)
        print("导入人格和插件；首次需要联网下载，可能持续数分钟…", flush=True)
        result = run(docker.compose_prefix + ["exec", "-T", "astrbot", "python", "-X", "utf8",
                     "/dsqq/pack/tools/import_dafeiyu.py", "--answers", internal + "/answers.json",
                     "--result-json", internal + "/result.json"], timeout=3600)
        output = inst.redact((result.stdout or "") + "\n" + (result.stderr or ""))
        for value in secret_values.values():
            output = output.replace(value, "***")
        private_write(inst.root / "import.log", output)
        summary = read_json(folder / "result.json", {"fatal": "导入器未生成结果"})
        failed = [item["name"] for item in summary.get("results", []) if not item.get("ok")]
        if result.returncode or summary.get("fatal") or failed:
            raise UserError(f"配置包未完全导入：{'、'.join(failed) or '导入器执行失败'}。查看 {inst.root / 'import.log'} 后重试 import-pack。")
    # Upstream group_chat_plus may ship an incomplete private_chat module.
    utils = inst.root / "astrbot/data/plugins/astrbot_plugin_group_chat_plus/private_chat/private_chat_utils"
    if (utils / "private_chat_session_guard.py").exists() and not (utils / "_session_guard.py").exists():
        private_write(utils / "_session_guard.py", "from .private_chat_session_guard import *\n")
        docker.compose("restart", "astrbot")
    print("人格与插件已导入。记忆/语音按本次参数启用；模型保持当前 New API 配置。")


def mcp(inst, args):
    api = AstrBot(inst)
    if args.action == "list":
        servers = api.call("GET", "/mcp/servers") or []
        # MCP configs can contain arbitrary bearer tokens; list metadata only.
        dump([{k: s.get(k) for k in ("name", "active", "connected", "tools")} for s in servers])
    elif args.action == "add":
        conf = read_json(Path(args.file).expanduser())
        if not isinstance(conf, dict) or not conf:
            raise UserError("MCP 配置必须是 JSON 对象。")
        if "mcpServers" in conf:
            servers = conf["mcpServers"]
            if not isinstance(servers, dict) or len(servers) != 1:
                raise UserError("一次只添加一个 MCP server。")
            conf = next(iter(servers.values()))
        if not isinstance(conf, dict):
            raise UserError("MCP server 配置必须是 JSON 对象。")
        api.call("POST", "/mcp/servers", {"name": args.name, "mcpServers": {args.name: conf}}, timeout=120)
        print("MCP 已添加并测试连接；用 tools list 检查工具。")
    elif args.action == "test":
        api.call("POST", "/mcp/servers/test", {"server_name": args.name}, timeout=120)
        print("MCP 连接正常。")
    else:
        api.call("DELETE", "/mcp/servers/by-name?" + urlencode({"server_name": args.name}))
        print("MCP 已移除。")


def tools_command(inst, args):
    api = AstrBot(inst)
    if args.action == "list":
        dump(api.call("GET", "/tools"))
    else:
        api.call("PATCH", "/tools/" + quote(args.name, safe="") + "/permission", {"permission": args.permission})
        print("工具权限已更新。")


def persona(inst, args):
    api = AstrBot(inst)
    if args.action == "list":
        dump(api.call("GET", "/personas"))
        return
    path = "/personas/" + quote(args.name, safe="")
    current = api.call("GET", path)
    if not isinstance(current, dict):
        raise UserError("人格不存在；先执行 import-pack 或确认名称。")
    body = {key: current[key] for key in ("system_prompt", "begin_dialogs", "tools", "skills",
                                        "folder_id", "sort_order", "custom_error_message") if key in current}
    if args.action == "prompt":
        text = Path(args.file).expanduser().read_text(encoding="utf-8").strip()
        if not text:
            raise UserError("人格文件不能为空。")
        body["system_prompt"] = text.replace("{OWNER_QQ}", inst.config["owner_qq"])
    else:
        body["tools"] = None if args.all else args.tool
    api.call("PUT", path, body)
    print("人格已更新；已有会话可发 /reset 重新开始。")


def doctor(inst, args):
    failed = False
    for command in ("docker", "colima", "qrencode"):
        location = shutil.which(command)
        print(f"{command}: {location or '未安装'}")
    try:
        if inst.path.exists():
            inst.load()
            Docker(inst).check()
        else:
            context = docker_context(args.context)
            if run(["docker", "--context", context, "info"], timeout=30).returncode:
                raise UserError(f"Docker context {context} 未运行。")
        print("Docker 引擎可用。")
    except UserError as exc:
        print(str(exc))
        failed = True
    print("无需 Docker Desktop；可使用 brew 安装的 Colima。详情见 README。")
    return int(failed)


def parser():
    ap = argparse.ArgumentParser(description="DSQQ macOS CLI：AstrBot + NapCat，全程终端操作")
    ap.add_argument("--root", default=os.environ.get("DSQQ_HOME", str(DEFAULT_ROOT)), help="独立安装数据目录")
    sub = ap.add_subparsers(dest="command", required=True)

    def model_args(p):
        p.add_argument("--base-url", help="OpenAI-compatible URL，例如 https://gateway.example/v1")
        p.add_argument("--model", help="网关实际支持的模型 ID")
        keys = p.add_mutually_exclusive_group()
        keys.add_argument("--api-key-env", help="保存 API Key 的环境变量名称")
        keys.add_argument("--api-key-file", help="保存 API Key 的文件路径")
        p.add_argument("--non-interactive", action="store_true")

    p = sub.add_parser("init", help="初始化配置（不启动容器，不调用付费模型）")
    model_args(p)
    p.add_argument("--owner-qq")
    p.add_argument("--context", help="本机 Docker context；默认保存当前 context")
    p.add_argument("--timezone", default="Asia/Shanghai")
    p.add_argument("--astrbot-image")
    p.add_argument("--napcat-image")
    model_args(sub.add_parser("configure", help="修改模型/网关；保留插件和人格"))
    p = sub.add_parser("doctor", help="检查本机 CLI 和容器运行环境")
    p.add_argument("--context")
    for name in ("install", "up", "restart"):
        p = sub.add_parser(name, help={"install": "拉取镜像、启动并导入配置包", "up": "启动或恢复服务", "restart": "重启服务"}[name])
        p.add_argument("--timeout", type=int, default=180)
        if name == "install":
            p.add_argument("--skip-pack", action="store_true", help="只安装运行环境")
    sub.add_parser("down", help="停止并删除本项目容器，保留所有数据")
    p = sub.add_parser("status", help="状态；未登录或未就绪时退出码为 1")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("logs", help="查看已脱敏的容器日志")
    p.add_argument("service", nargs="?", choices=("astrbot", "napcat"))
    p.add_argument("--follow", "-f", action="store_true")
    p.add_argument("--tail", type=int, default=100)
    p = sub.add_parser("login", help="在终端显示二维码并等待登录")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--timeout", type=int, default=300)
    p = sub.add_parser("import-pack", help="导入/重试大肥鱼人格与插件")
    p.add_argument("--memory", choices=("none", "siliconflow"), default="none")
    p.add_argument("--voice", choices=("none", "doubao"), default="none")
    sub.add_parser("test-model", help="通过 AstrBot 测试模型（会发起真实 API 请求）")
    sub.add_parser("models", help="列出配置的模型（隐藏凭证）")
    p = sub.add_parser("mcp", help="管理 MCP 服务")
    actions = p.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    for action in ("add", "test", "remove"):
        q = actions.add_parser(action)
        q.add_argument("name")
        if action == "add":
            q.add_argument("--file", required=True)
    p = sub.add_parser("tools", help="列出工具和设置权限")
    actions = p.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    q = actions.add_parser("permission")
    q.add_argument("name")
    q.add_argument("permission", choices=("admin", "member"))
    p = sub.add_parser("persona", help="管理人格提示词和工具选择")
    actions = p.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    q = actions.add_parser("prompt")
    q.add_argument("name")
    q.add_argument("--file", required=True)
    q = actions.add_parser("tools")
    q.add_argument("name")
    group = q.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true")
    group.add_argument("--tool", nargs="*", help="工具名列表；空列表表示禁用所有工具")
    p = sub.add_parser("uninstall", help="删除本项目容器；加 --purge 才删除数据")
    p.add_argument("--purge", action="store_true")
    p.add_argument("--yes", action="store_true")
    return ap


def dispatch(inst, args):
    docker = Docker(inst)
    command = args.command
    if command in ("up", "install"):
        up(inst, args, pull=command == "install")
        if command == "install" and not args.skip_pack:
            args.memory = args.voice = "none"
            import_pack(inst, args)
    elif command in ("down", "restart"):
        docker.check()
        docker.compose(command)
        if command == "restart":
            wait_ready(inst, args.timeout)
    elif command == "configure":
        configure(inst, args)
    elif command == "status":
        return status(inst, args)
    elif command == "login":
        login(inst, args)
    elif command == "logs":
        argv = ["logs", "--no-color", "--tail", str(args.tail)]
        if args.follow:
            argv.append("--follow")
        if args.service:
            argv.append(args.service)
        docker.stream(*argv)
    elif command == "import-pack":
        import_pack(inst, args)
    elif command == "test-model":
        result = AstrBot(inst).call("POST", "/providers/test", {"provider_id": MODEL_ID}, timeout=180)
        dump(json.loads(inst.redact(json.dumps(result))))
        if isinstance(result, dict) and (result.get("status") in ("error", "failed", "unavailable") or result.get("error")):
            return 1
    elif command == "models":
        providers = AstrBot(inst).call("GET", "/providers") or []
        if isinstance(providers, dict):
            providers = providers.get("providers", [])
        dump([{k: item.get(k) for k in ("id", "model", "enable", "provider_source_id")}
              for item in providers if isinstance(item, dict)])
    elif command == "mcp":
        mcp(inst, args)
    elif command == "tools":
        tools_command(inst, args)
    elif command == "persona":
        persona(inst, args)
    elif command == "uninstall":
        if args.purge and not args.yes:
            raise UserError(f"彻底删除 {inst.root} 的配置、聊天与 QQ 登录数据需要 --purge --yes。")
        docker.check()
        docker.compose("down")
        if args.purge:
            # Only delete a validated DSQQ root, never arbitrary user directories.
            inst.load()
            shutil.rmtree(inst.root)
            print("已删除此 DSQQ 安装的数据；镜像、Colima 和其他项目未删除。")
        else:
            print("容器已移除，数据已保留。")
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    inst = Installation(args.root)
    try:
        if args.command == "doctor":
            return doctor(inst, args)
        if args.command == "init":
            init(inst, args)
            return 0
        if args.command in ("status", "logs", "models", "test-model", "login"):
            inst.load()
            return dispatch(inst, args)
        with inst.lock():
            inst.load()
            return dispatch(inst, args)
    except KeyboardInterrupt:
        print("\n已取消当前命令；后台服务保持原状态。", file=sys.stderr)
        return 130
    except (UserError, OSError, ValueError, KeyError, TypeError) as exc:
        print("错误：" + inst.redact(str(exc)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
