"""Scoped Docker Compose operations and authenticated service health checks."""
import hashlib
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from .state import UserError, read_json, write_json


class HttpError(UserError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"管理接口返回 HTTP {code}")


def cached_session(path, identity):
    try:
        cached = read_json(path)
        if cached.get("identity") == identity and cached.get("expires", 0) > time.time():
            return cached.get("token")
    except (UserError, AttributeError, TypeError):
        pass  # Cache corruption must not prevent a fresh login.
    return None


def run(argv, *, capture=True, timeout=120, input_text=None):
    try:
        return subprocess.run(argv, capture_output=capture, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, input=input_text, check=False)
    except FileNotFoundError as exc:
        raise UserError(f"找不到命令：{argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise UserError(f"命令超时：{argv[0]}；可重试。") from exc


def docker_context(selected=None):
    if not shutil.which("docker"):
        raise UserError("缺少 Docker CLI；先按 README 安装 docker、docker-compose 和 colima。")
    if not selected:
        result = run(["docker", "context", "show"])
        if result.returncode:
            raise UserError("无法读取 Docker context。")
        selected = result.stdout.strip()
    result = run(["docker", "context", "inspect", selected, "--format", "{{.Endpoints.docker.Host}}"])
    if result.returncode or not result.stdout.strip().startswith("unix://"):
        raise UserError("DSQQ 需要本机 Unix socket Docker context，不能使用远程引擎的 bind mount。")
    return selected


class Docker:
    def __init__(self, inst):
        self.inst = inst
        self.prefix = ["docker", "--context", inst.config["docker_context"]]
        self.compose_prefix = self.prefix + ["compose", "--project-name", inst.config["project"],
                                           "--project-directory", str(inst.root),
                                           "--file", str(inst.root / "compose.json")]

    def check(self):
        result = run(self.prefix + ["info", "--format", "{{.OSType}}"], timeout=30)
        if result.returncode or result.stdout.strip() != "linux":
            raise UserError(f"Docker context {self.inst.config['docker_context']} 未就绪。启动对应 Colima profile 后重试。")
        if run(self.prefix + ["compose", "version"]).returncode:
            raise UserError("缺少 Docker Compose 插件；查看 README 的安装命令。")

    def compose(self, *args, timeout=120, capture=True):
        result = run(self.compose_prefix + list(args), capture=capture, timeout=timeout)
        if result.returncode:
            detail = self.inst.redact((result.stderr or result.stdout or "").strip())
            raise UserError(f"Docker Compose {args[0]} 失败：{detail[-2000:]}")
        return result.stdout or ""

    def processes(self):
        raw = self.compose("ps", "--all", "--format", "json").strip()
        if not raw:
            return []
        if raw.startswith("["):
            return json.loads(raw)
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def running(self):
        return [p["Service"] for p in self.processes() if p.get("State") == "running"]

    def stream(self, *args):
        proc = subprocess.Popen(self.compose_prefix + list(args), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        try:
            for line in proc.stdout:
                print(self.inst.redact(line), end="", flush=True)
            if proc.wait():
                raise UserError("命令失败；见上方已脱敏输出。")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def request_json(url, body=None, headers=None, timeout=10, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})},
                                 method=method)
    try:
        # Local management APIs must not go through a user's HTTP proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise HttpError(exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise UserError("管理接口暂不可达或返回格式错误。") from exc


class AstrBot:
    def __init__(self, inst):
        self.base = f"http://127.0.0.1:{inst.config['ports']['dashboard']}/api/v1"
        self.password = inst.config["secrets"]["dashboard_password"]
        self.cache_path = inst.root / ".astrbot-session.json"
        self.identity = hashlib.sha256((self.base + self.password).encode()).hexdigest()
        self.token = cached_session(self.cache_path, self.identity)

    def login(self):
        if self.token:
            try:
                # An authenticated request is still required for readiness, even with a cache.
                request_json(self.base + "/providers", headers={"Authorization": f"Bearer {self.token}"})
                return
            except HttpError as exc:
                if exc.code not in (401, 403):
                    raise
                self.token = None
                self.cache_path.unlink(missing_ok=True)
        for password in (self.password, hashlib.md5(self.password.encode()).hexdigest()):
            try:
                response = request_json(self.base + "/auth/login", {"username": "dafeiyu", "password": password})
                self.token = (response.get("data") or {}).get("token")
                if self.token:
                    write_json(self.cache_path, {"identity": self.identity, "token": self.token,
                                                 "expires": time.time() + 900})
                    return
            except HttpError as exc:
                if exc.code == 429:
                    raise UserError("AstrBot 登录限流，请稍后重试。") from exc
            except UserError:
                continue
        raise UserError("AstrBot 未就绪或管理密码已被外部修改。")

    def call(self, method, path, body=None, timeout=30):
        if not self.token:
            self.login()
        try:
            response = request_json(self.base + path, body, {"Authorization": f"Bearer {self.token}"},
                                    timeout=timeout, method=method)
        except HttpError as exc:
            if exc.code != 401:
                raise
            self.token = None
            self.cache_path.unlink(missing_ok=True)
            self.login()
            response = request_json(self.base + path, body, {"Authorization": f"Bearer {self.token}"},
                                    timeout=timeout, method=method)
        if response.get("status") == "error":
            raise UserError("AstrBot 接口执行失败：" + str(response.get("message", "未知错误")))
        return response.get("data")


class NapCat:
    def __init__(self, inst):
        self.base = f"http://127.0.0.1:{inst.config['ports']['napcat']}/api"
        self.secret = inst.config["secrets"]["napcat_token"]
        self.cache_path = inst.root / ".napcat-session.json"
        self.identity = hashlib.sha256((self.base + self.secret).encode()).hexdigest()
        self.token = cached_session(self.cache_path, self.identity)

    def call(self, path, body=None):
        for attempt in range(2):
            if not self.token:
                login = request_json(self.base + "/auth/login", {
                    "hash": hashlib.sha256((self.secret + ".napcat").encode()).hexdigest()})
                if login.get("code") != 0:
                    raise UserError("NapCat WebUI 认证失败或登录过于频繁；稍后重试。")
                self.token = login["data"]["Credential"]
                # Share a short-lived session between CLI commands to avoid WebUI login throttling.
                write_json(self.cache_path, {"identity": self.identity, "token": self.token,
                                             "expires": time.time() + 3300})
            response = request_json(self.base + path, body or {}, {"Authorization": f"Bearer {self.token}"})
            if response.get("code") == 0:
                return response.get("data") or {}
            if attempt == 0 and "Unauthorized" in str(response.get("message")):
                self.token = None
                self.cache_path.unlink(missing_ok=True)
                continue
            raise UserError("NapCat 接口执行失败：" + str(response.get("message", "未知错误")))


def wait_ready(inst, timeout=180):
    deadline = time.monotonic() + timeout
    astrbot, napcat = AstrBot(inst), NapCat(inst)
    last = ""
    while time.monotonic() < deadline:
        try:
            astrbot.login()
            napcat.call("/QQLogin/CheckLoginStatus")
            return
        except UserError as exc:
            last = str(exc)
            time.sleep(2)
    raise UserError(f"服务启动超时：{last} 使用 dsqq logs 查看；再次 up 可继续。")
