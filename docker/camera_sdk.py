"""首次启动下载固定 SDK，校验后安装到私有卷；绝不启动摄像头。"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
BASE_SITE = Path('/opt/runtime/lib/python3.12/site-packages')
NOTICE = ('本项目仅用于非商业用途。SDK 适用 Xiaomi Miloco License Agreement，'
          '并非 MIT 授权；非商业声明及自动下载不扩大原许可范围。'
          'Friday 集成用途的额外授权尚未确认。\n')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def regular(path):
    """缓存不允许经过符号链接，也不信任其他账号或可公开写入的文件。"""
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('camera_sdk_unsafe_path')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('camera_sdk_unsafe_file')


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not newurl.startswith('https://'):
            raise ValueError('camera_sdk_insecure_redirect')
        return super().redirect_request(request, fp, code, message, headers, newurl)


def download(lock, target):
    """固定 HTTPS 源，限制总大小与耗时；失败不留下可用缓存标记。"""
    if not lock['archive_url'].startswith('https://github.com/XiaoMi/xiaomi-miloco/releases/download/'):
        raise ValueError('camera_sdk_untrusted_source')
    opener = urllib.request.build_opener(HTTPSRedirect())
    for attempt in range(3):
        try:
            started = time.monotonic()
            with opener.open(lock['archive_url'], timeout=30) as response, target.open('wb') as output:
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > lock['archive_max_bytes'] or time.monotonic() - started > 180:
                        raise ValueError('camera_sdk_download_limit')
                    output.write(chunk)
            if digest(target) != lock['archive_sha256']:
                raise ValueError('camera_sdk_archive_hash_mismatch')
            return
        except (OSError, ValueError):
            target.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def extract_wheel(archive, wheel, lock):
    """只读取固定归档中的单个普通文件，不解包其他服务或任意路径。"""
    if digest(archive) != lock['archive_sha256']:
        raise ValueError('camera_sdk_archive_hash_mismatch')
    with tarfile.open(archive, 'r:gz') as source:
        matches = [m for m in source if m.name in (lock['wheel'], './' + lock['wheel'])]
        if len(matches) != 1 or not matches[0].isfile() or matches[0].size > 32 * 1024 * 1024:
            raise ValueError('camera_sdk_invalid_archive')
        with source.extractfile(matches[0]) as stream, wheel.open('wb') as target:
            shutil.copyfileobj(stream, target)
    if digest(wheel) != lock['wheel_sha256']:
        raise ValueError('camera_sdk_wheel_hash_mismatch')


def wheel_files(wheel, lock):
    """固定 wheel 仅允许包文件和分发元数据，不接受安装钩子或链接。"""
    regular(wheel)
    if digest(wheel) != lock['wheel_sha256']:
        raise ValueError('camera_sdk_wheel_hash_mismatch')
    result = {}
    with zipfile.ZipFile(wheel) as source:
        total = 0
        for member in source.infolist():
            path = PurePosixPath(member.filename)
            if (path.is_absolute() or '..' in path.parts or '\\' in member.filename
                    or not path.parts or path.parts[0] not in ('miot', f"miloco_miot-{lock['version']}.dist-info")
                    or stat.S_ISLNK(member.external_attr >> 16)):
                raise ValueError('camera_sdk_unsafe_wheel')
            if member.is_dir():
                continue
            total += member.file_size
            if total > 64 * 1024 * 1024 or member.filename in result:
                raise ValueError('camera_sdk_unsafe_wheel')
            result[member.filename] = source.read(member)
    return result


def validate(folder, lock):
    """重启时以已校验 wheel 复核安装内容，缓存损坏时明确失败。"""
    files = wheel_files(folder / lock['wheel'], lock)
    site = folder / 'lib/python3.12/site-packages'
    expected = set(files) | {'friday-runtime.pth'}
    actual = {p.relative_to(site).as_posix() for p in site.rglob('*') if p.is_file() or p.is_symlink()}
    if actual != expected:
        raise ValueError('camera_sdk_cache_contents_changed')
    for relative, data in files.items():
        path = site / relative
        regular(path)
        if hashlib.sha256(data).hexdigest() != digest(path):
            raise ValueError('camera_sdk_cache_hash_mismatch')
    pth = site / 'friday-runtime.pth'
    regular(pth)
    if pth.read_text() != str(BASE_SITE) + '\n':
        raise ValueError('camera_sdk_runtime_path_changed')
    for name in ('LICENSE.md', 'NOTICE.txt', 'installed.json', 'pyvenv.cfg'):
        regular(folder / name)
    if digest(folder / 'LICENSE.md') != lock['license_sha256'] or (folder / 'NOTICE.txt').read_text() != NOTICE:
        raise ValueError('camera_sdk_notice_changed')
    if json.loads((folder / 'installed.json').read_text()) != lock:
        raise ValueError('camera_sdk_lock_changed')
    # 只验证系统解释器链接；缓存中不得替换为自行生成的可执行文件。
    python = folder / 'bin/python'
    if not python.is_symlink() or python.resolve() != Path('/usr/local/bin/python3.12').resolve():
        raise ValueError('camera_sdk_interpreter_changed')
    if (folder / 'pyvenv.cfg').read_text() != 'home = /usr/local/bin\ninclude-system-site-packages = false\nversion = 3.12\n':
        raise ValueError('camera_sdk_venv_changed')


def ensure(data, *, fetch=download):
    lock = json.loads((HERE / 'camera-sdk-lock.json').read_text())
    license_path = HERE / 'licenses/Xiaomi-Miloco-LICENSE.md'
    if digest(license_path) != lock['license_sha256']:
        raise ValueError('camera_sdk_license_hash_mismatch')
    root = data / 'camera-sdk'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(p.is_symlink() for p in (root, *root.parents)) or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError('camera_sdk_unsafe_directory')
    descriptor = os.open(root / 'install.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        regular(root / 'install.lock')
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        final = root / lock['version']
        if final.exists() or final.is_symlink():
            validate(final, lock)
            print(json.dumps({'camera_sdk': 'cached_verified', 'version': lock['version']}), flush=True)
            return final
        print(json.dumps({'camera_sdk': 'downloading', 'version': lock['version'], 'notice': NOTICE.strip()}, ensure_ascii=False), flush=True)
        # 中断遗留目录只由下一轮持锁的安装器回收，不接触其他数据。
        for stale in root.glob('.install-*'):
            if stale.is_symlink():
                raise ValueError('camera_sdk_unsafe_partial')
            shutil.rmtree(stale)
        with tempfile.TemporaryDirectory(prefix='.install-', dir=root) as temporary:
            folder = Path(temporary)
            archive = folder / 'release.tar.gz'
            fetch(lock, archive)
            wheel = folder / lock['wheel']
            extract_wheel(archive, wheel, lock)
            archive.unlink()
            files = wheel_files(wheel, lock)
            site = folder / 'lib/python3.12/site-packages'
            site.mkdir(parents=True)
            for relative, content in files.items():
                target = site / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            (site / 'friday-runtime.pth').write_text(str(BASE_SITE) + '\n')
            (folder / 'bin').mkdir()
            (folder / 'bin/python').symlink_to('/usr/local/bin/python3.12')
            (folder / 'pyvenv.cfg').write_text('home = /usr/local/bin\ninclude-system-site-packages = false\nversion = 3.12\n')
            shutil.copyfile(license_path, folder / 'LICENSE.md')
            (folder / 'NOTICE.txt').write_text(NOTICE)
            (folder / 'installed.json').write_text(json.dumps(lock, indent=2) + '\n')
            validate(folder, lock)
            # 不执行 SDK，不连接摄像头；只确认基础环境能够满足分发依赖。
            # 使用实际解释器检查其可见依赖；uv 的静态检查不会遍历复用环境的 .pth。
            probe = ('from importlib.metadata import requires,version\n'
                     'from packaging.requirements import Requirement\n'
                     'for value in requires("miloco-miot"):\n'
                     ' requirement=Requirement(value)\n'
                     ' if requirement.marker is None or requirement.marker.evaluate({"extra":""}):\n'
                     '  assert version(requirement.name) in requirement.specifier, requirement.name\n')
            subprocess.run([str(folder / 'bin/python'), '-B', '-I', '-c', probe], check=True, timeout=60)
            folder.rename(final)
        print(json.dumps({'camera_sdk': 'installed_verified', 'version': lock['version']}), flush=True)
        return final
    finally:
        os.close(descriptor)
