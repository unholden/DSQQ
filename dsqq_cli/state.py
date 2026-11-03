"""Private files and a cross-process lock for a single installation."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import tempfile


class UserError(Exception):
    pass


def read_json(path, default=None):
    if not path.exists():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise UserError(f"无法读取 JSON：{path}（{type(exc).__name__}）") from exc


def private_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".dsqq-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path, value):
    private_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class Installation:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / "dsqq.json"
        self.config = None

    def load(self):
        if not self.path.is_file():
            raise UserError("此目录尚未初始化；先运行 dsqq init。")
        self.config = read_json(self.path)
        if self.config.get("schema") != 1 or self.config.get("kind") != "dsqq-cli":
            raise UserError("不支持的安装状态格式，拒绝修改此目录。")
        return self.config

    def save(self):
        write_json(self.path, self.config)

    @contextlib.contextmanager
    def lock(self, creating=False):
        if creating:
            if self.root.exists() and not self.path.exists():
                if any(p.name != ".dsqq.lock" for p in self.root.iterdir()):
                    raise UserError("初始化需要空目录，不能覆盖现有文件。")
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        elif not self.path.is_file():
            raise UserError("此目录尚未初始化；先运行 dsqq init。")
        with open(self.root / ".dsqq.lock", "a", encoding="utf-8") as lock:
            os.chmod(lock.name, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UserError("另一个 DSQQ 命令正在修改此安装，请稍后重试。") from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def redact(self, text):
        if not self.config:
            return text
        values = [self.config.get("model", {}).get("api_key", "")]
        values.extend(self.config.get("secrets", {}).values())
        for value in sorted((v for v in values if isinstance(v, str) and v), key=len, reverse=True):
            text = text.replace(value, "***")
        return text
