"""Purpose- and endpoint-bound local NAS secrets. No transport, search or logging."""
import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path

from .contracts import NASConnectionConfig, NASCredentials

_PURPOSE='friday.nas.read-only.v1'

def binding(config: NASConnectionConfig) -> str:
    fields={key:getattr(config,key) for key in
            ('source_ref','transport','host','share','root','require_encryption',
             'host_key_sha256','sftp_root_confined','credential_ref')}
    fields['port']=config.effective_port
    return hashlib.sha256(json.dumps(fields,sort_keys=True,separators=(',',':')).encode()).hexdigest()

class LocalNASCredentials:
    """Only explicit local setup can write; construction never reads a secret."""
    def __init__(self, directory):
        self.directory=Path(directory).absolute()

    def _directory_fd(self, *, create=False):
        fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
        try:
            parts=self.directory.parts[1:]
            for index,part in enumerate(parts):
                if create:
                    try:os.mkdir(part,0o700,dir_fd=fd)
                    except FileExistsError:pass
                next_fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                os.close(fd);fd=next_fd
                info=os.fstat(fd)
                if index>=len(parts)-2 and (info.st_uid!=os.getuid() or info.st_mode&0o077):
                    raise ValueError('nas_private_store_unavailable')
            return fd
        except BaseException:
            os.close(fd);raise

    @staticmethod
    def _name(config):
        return hashlib.sha256(config.credential_ref.encode()).hexdigest()+'.json'

    def save(self, config, credentials):
        credentials.payload()
        payload={'version':1,'purpose':_PURPOSE,'binding':binding(config),**credentials.payload()}
        directory=self._directory_fd(create=True);temporary='.'+secrets.token_hex(16)+'.tmp'
        try:
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
            with os.fdopen(fd,'w',encoding='utf-8') as output:
                json.dump(payload,output,ensure_ascii=False);output.flush();os.fsync(output.fileno())
            os.replace(temporary,self._name(config),src_dir_fd=directory,dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:os.unlink(temporary,dir_fd=directory)
            except FileNotFoundError:pass
            os.close(directory)

    def resolve(self, config, *, purpose):
        if purpose!=_PURPOSE:return None
        try:
            directory=self._directory_fd()
            try:fd=os.open(self._name(config),os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
            finally:os.close(directory)
            with os.fdopen(fd,'rb') as source:
                info=os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid()
                        or info.st_mode&0o077 or info.st_size>65536):return None
                raw=source.read(65537)
            value=json.loads(raw)
            if (set(value)!={'version','purpose','binding','username','password'}
                    or value['version']!=1 or value['purpose']!=purpose
                    or not isinstance(value['binding'],str)
                    or not hmac.compare_digest(value['binding'],binding(config))):return None
            result=NASCredentials(value['username'],value['password']);result.payload();return result
        except (OSError,ValueError,TypeError,KeyError,AttributeError):
            return None
