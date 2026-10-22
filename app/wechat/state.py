from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .client import ProtocolError, trusted_url


def atomic_write(path: Path, content: bytes):
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class State:
    def __init__(self, directory: Path):
        if directory.is_symlink():
            raise ProtocolError("状态目录不能是符号链接。")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.stat().st_uid != os.getuid():
            raise ProtocolError("状态目录不属于当前用户。")
        directory.chmod(0o700)
        self.directory = directory

    @contextmanager
    def lock(self):
        if (self.directory / 'migrated.json').exists():
            raise ProtocolError('此帐号已迁到服务器；禁止启动第二个轮询器。回退须先停服务器并同步最新状态。')
        fd = os.open(self.directory / "probe.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProtocolError("已有微信进程占用同一帐号状态，请先停止它再启动。") from None
            yield
        finally:
            os.close(fd)

    def read(self, name, default=None):
        path = self.directory / name
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return default
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ProtocolError("状态文件必须属于当前用户且权限为 600。")
            if info.st_size > 4 * 1024 * 1024:
                raise ProtocolError("状态文件过大，已停止以保留现场。")
            try:
                result = json.load(stream)
            except (ValueError, UnicodeError):
                raise ProtocolError("状态文件损坏，已停止以保留现场。") from None
            if not isinstance(result, dict):
                raise ProtocolError("状态文件格式无效。")
            return result

    def write(self, name, value):
        atomic_write(self.directory / name, json.dumps(value, ensure_ascii=False).encode())

    def account(self):
        account = self.read("account.json")
        if account is not None:
            validate_account(account)
        return account


def validate_account(account):
    for key in ("token", "bot_id", "owner", "base_url"):
        if not isinstance(account.get(key), str) or not account[key].strip():
            raise ProtocolError("登录结果缺少 Bot 凭证或可信扫码者身份，未绑定。")
    trusted_url(account["base_url"], api=True)


def confirmed_account(data):
    account = {"token": data.get("bot_token"), "bot_id": data.get("ilink_bot_id"),
               "owner": data.get("ilink_user_id"), "base_url": data.get("baseurl")}
    validate_account(account)
    return account
