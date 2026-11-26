"""在现有 Miloco Python 环境中安装或移除本机授权巡检。"""

import argparse
import ast
import fcntl
import importlib.util
import os
import tempfile
from pathlib import Path

OLD = "        self._token_refresh_task = asyncio.create_task(self._start_token_refresh_task())"
NEW = (
    "        from .friday_auth_recovery import supervise\n"
    "        self._token_refresh_task = asyncio.create_task(supervise(self))"
)


def write(path, body, mode):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install(directory, uninstall=False, check_only=False):
    path = directory / "client.py"
    helper = directory / "friday_auth_recovery.py"
    shipped = Path(__file__).with_name(helper.name).read_bytes()
    with (directory / ".friday-recovery.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = path.read_bytes()
        newline = "\r\n" if b"\r\n" in data else "\n"
        src = data.decode().replace("\r\n", "\n")
        if uninstall:
            if NEW not in src:
                return
            changed = src.replace(NEW, OLD, 1)
        else:
            if src.count(OLD) + src.count(NEW) != 1 or any(
                k not in src
                for k in (
                    "_token_refresh_lock",
                    "_auth_health",
                    "_oauth_info",
                    "_kv_repo",
                    "MIOT_REFRESH_INFLIGHT_KEY",
                    "async def _start_token_refresh_task(",
                )
            ):
                raise SystemExit("当前 Miloco 的续期接口不兼容，已保留原安装。")
            changed = src.replace(OLD, NEW, 1)
            compile(shipped, str(helper), "exec")
        ast.parse(changed)
        helper_changed = not helper.is_file() or helper.read_bytes() != shipped
        if check_only:
            return changed != src or helper_changed
        if not uninstall and helper_changed:
            write(helper, shipped, 0o644)
        if changed != src:
            write(path, changed.replace("\n", newline).encode(), path.stat().st_mode & 0o777)
        if uninstall and helper.exists() and helper.read_bytes() == shipped:
            helper.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--uninstall", action="store_true")
    mode.add_argument("--check", action="store_true")
    opts = parser.parse_args()
    spec = importlib.util.find_spec("miloco")
    if not spec or not spec.submodule_search_locations:
        parser.error("请使用 Miloco 后台的 Python 运行此脚本")
    needs_install = install(
        Path(next(iter(spec.submodule_search_locations))) / "miot", opts.uninstall, opts.check
    )
    if opts.check:
        raise SystemExit(10 if needs_install else 0)
    print("处理完成。重启 Miloco 后台后生效。")
