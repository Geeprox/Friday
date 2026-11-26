"""把恢复组件维护在 Miloco 环境之外，由本机服务管理器在升级后重新核对。"""

import argparse
import fcntl
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

if __package__:
    from .install import write
else:
    from install import write

LABEL = "local.friday.miloco-components"
MAC = sys.platform == "darwin"
HOME = Path.home()
BASE = (
    HOME / "Library/Application Support/Friday/miloco-components"
    if MAC
    else Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "friday/miloco-components"
)
FILES = ("install.py", "friday_auth_recovery.py", "lifecycle.py")
PROFILE_KEYS = ("MILOCO_RUNTIME_ENV", "MILOCO_HOME")


def replace(path, body, mode=0o600):
    if not path.is_file() or path.read_bytes() != body:
        write(path, body, mode)


def run(args, **kwargs):
    return subprocess.run(args, check=True, timeout=120, **kwargs)


def cli_json(cli, *args):
    return json.loads(run([cli, *args], capture_output=True).stdout)


def running_pid(cli):
    status = cli_json(cli, "service", "status")
    if status.get("managed") and status.get("running") and isinstance(status.get("pid"), int):
        return status["pid"]
    return None


def installing():
    # 官方网页升级和 agent 安装可能多次启停后台；等待安装进程完全退出。
    processes = run(["ps", "-U", str(os.getuid()), "-o", "command="], capture_output=True, text=True).stdout
    for line in processes.splitlines():
        args = line.split()
        if len(args) < 2:
            continue
        program = Path(args[0]).name
        if program not in {"sh", "bash", "dash", "zsh"} and not program.startswith("python"):
            continue
        if args[1] in {"-lc", "-c"} and ".upgrade-install.sh" in line and "miloco-cli service start" in line:
            return True
        if {"--agent-prepare", "--agent-finish"}.intersection(args) and any(
            Path(arg).name.endswith(("install.sh", "install.py")) for arg in args[1:]
        ):
            return True
    return False


def reconcile(cli, immediate=False):
    pending = BASE / "pending.json"
    with (BASE / "reconcile.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not immediate and installing():
            return "upgrading"
        if not Path(cli).is_file() and not immediate:
            return "not_installed"
        py = cli_json(cli, "config", "get", "server.python_bin")["value"]
        if not isinstance(py, str) or not Path(py).is_absolute():
            raise ValueError("Miloco 后台解释器未配置")
        command = [py, str(BASE / "install.py")]
        check = subprocess.run(command + ["--check"], timeout=30)
        if check.returncode not in (0, 10):
            raise RuntimeError("恢复组件兼容性检查失败，请查看本机服务日志")
        record = json.loads(pending.read_text()) if pending.exists() else {}
        if check.returncode == 10:
            pid = running_pid(cli)
            # 先保存待重载对象；中途退出时下次检查仍能完成剩余步骤。
            record = {"pid": pid}
            replace(pending, json.dumps(record).encode())
            run(command)
        if record and record["pid"] and running_pid(cli) == record["pid"]:
            if not immediate and installing():
                return "upgrading"
            run([cli, "service", "restart"])
        pending.unlink(missing_ok=True)
        return "ready"


def systemd_arg(value):
    return (
        '"' + str(value).replace("%", "%%").replace("$", "$$").replace("\\", "\\\\").replace('"', '\\"') + '"'
    )


def register(cli):
    program = ["/usr/bin/python3", str(BASE / "lifecycle.py"), "check", "--cli", cli]
    if MAC:
        path = HOME / "Library/LaunchAgents" / (LABEL + ".plist")
        path.parent.mkdir(parents=True, exist_ok=True)
        body = plistlib.dumps(
            dict(
                Label=LABEL,
                ProgramArguments=program,
                StartInterval=120,
                RunAtLoad=True,
                StandardErrorPath=str(BASE / "maintenance.log"),
            )
        )
        target = f"gui/{os.getuid()}"
        loaded = (
            subprocess.run(["launchctl", "print", target + "/" + LABEL], capture_output=True).returncode == 0
        )
        if loaded and path.exists() and path.read_bytes() == body:
            return
        if loaded:
            run(["launchctl", "bootout", target + "/" + LABEL])
        replace(path, body)
        run(["launchctl", "bootstrap", target, str(path)])
    else:
        directory = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "systemd/user"
        directory.mkdir(parents=True, exist_ok=True)
        service = (
            "[Unit]\nDescription=Friday Miloco component maintenance\n[Service]\nType=oneshot\nExecStart="
        )
        replace(
            directory / (LABEL + ".service"), (service + " ".join(map(systemd_arg, program)) + "\n").encode()
        )
        timer = (
            "[Unit]\nDescription=Check Miloco after installation and upgrades\n[Timer]\n"
            "OnStartupSec=30s\nOnUnitInactiveSec=2min\n[Install]\nWantedBy=timers.target\n"
        )
        replace(directory / (LABEL + ".timer"), timer.encode())
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", "--now", LABEL + ".timer"])


def unregister():
    if MAC:
        target = f"gui/{os.getuid()}/{LABEL}"
        if subprocess.run(["launchctl", "print", target], capture_output=True).returncode == 0:
            run(["launchctl", "bootout", target])
        (HOME / "Library/LaunchAgents" / (LABEL + ".plist")).unlink(missing_ok=True)
    else:
        directory = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "systemd/user"
        if (directory / (LABEL + ".timer")).exists():
            run(["systemctl", "--user", "disable", "--now", LABEL + ".timer"])
            run(["systemctl", "--user", "stop", LABEL + ".service"])
        for suffix in (".service", ".timer"):
            (directory / (LABEL + suffix)).unlink(missing_ok=True)
        run(["systemctl", "--user", "daemon-reload"])


def main():
    global BASE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("enable", "check", "disable"))
    parser.add_argument("--cli", default=shutil.which("miloco-cli") or str(HOME / ".local/bin/miloco-cli"))
    args = parser.parse_args()
    if args.action == "disable":
        unregister()
        return
    cli = os.path.abspath(args.cli)
    os.environ["PATH"] = os.pathsep.join([str(Path(cli).parent), os.environ.get("PATH", os.defpath)])
    if args.action == "check":
        BASE = Path(__file__).resolve().parent
        profile = json.loads((BASE / "profile.json").read_text())
        for key in PROFILE_KEYS:
            if key in profile:
                os.environ[key] = profile[key]
            else:
                os.environ.pop(key, None)
    if args.action == "enable":
        if not (MAC or sys.platform.startswith("linux")):
            parser.error("支持 macOS 和 Linux")
        BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (BASE / "reconcile.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            profile = {
                key: str(Path(os.environ[key]).expanduser().absolute())
                for key in PROFILE_KEYS
                if os.environ.get(key)
            }
            replace(BASE / "profile.json", json.dumps(profile).encode())
            for name in FILES:
                replace(BASE / name, Path(__file__).with_name(name).read_bytes())
        register(cli)
    status_file = BASE / "maintenance-status.json"
    try:
        result = reconcile(cli, immediate=args.action == "enable")
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        replace(status_file, b'{"status":"check_failed"}')
        raise
    replace(status_file, json.dumps({"status": result}).encode())
    if args.action == "enable":
        print("Miloco 恢复组件和升级维护已启用。")
    return result


if __name__ == "__main__":
    main()
