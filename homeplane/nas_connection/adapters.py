"""Direct SMB2/3 and SFTP read clients. Imports stay in the isolated worker.

Server account/share confinement is the security boundary. SMB reparse points and
DFS shares are rejected; SFTP needs a pinned, server-confined account rooted at /.
Metadata versions detect observed changes, not same-metadata content substitution.
"""

import base64
import hashlib
import hmac
import stat as statmod
import time
import uuid
from collections import deque
from datetime import UTC, datetime

from .contracts import MAX_PAGE, MAX_RANGE, NASConnectionError, relative_path


def signature(*values):
    return hashlib.sha256(repr(values).encode()).hexdigest()


def stamp(value):
    if isinstance(value, int):
        # FileBasicInformation uses FILETIME integers; directory fields use datetime.
        from smbprotocol.structure import DateTimeField

        field = DateTimeField(size=8)
        field.set_value(value)
        value = field.get_value()
    if value is None:
        return None
    delta = value.replace(tzinfo=UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return ((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1000


class Reader:
    def __init__(self):
        self.cursors = {}

    def list_page(self, path, cursor, limit):
        self.prune_cursors()
        relative_path(path, allow_root=True)
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
            raise NASConnectionError("nas_invalid_request")
        if cursor:
            saved = self.cursors.pop(cursor, None)
            if saved is None or saved[0] != path:
                raise NASConnectionError("nas_cursor_expired")
            _, iterator, visited, _ = saved
        else:
            if len(self.cursors) >= 8:
                raise NASConnectionError("nas_budget_exceeded")
            iterator, visited = iter(self.iter_entries(path)), 0
        entries, complete, retained = [], False, False
        try:
            while len(entries) < limit:
                try:
                    entry = next(iterator)
                except StopIteration:
                    complete = True
                    break
                visited += 1
                if visited > 4096:
                    raise NASConnectionError("nas_budget_exceeded")
                if entry is not None:
                    entries.append(entry)
            token = None
            if not complete:
                token = uuid.uuid4().hex
                self.cursors[token] = (path, iterator, visited, time.monotonic() + 120)
                retained = True
            return {"entries": entries, "cursor": token, "complete": complete}
        finally:
            if not retained:
                iterator.close()

    def close_cursors(self):
        saved, self.cursors = self.cursors, {}
        for _, iterator, _, _ in saved.values():
            iterator.close()

    def prune_cursors(self):
        for key, record in list(self.cursors.items()):
            if record[3] < time.monotonic():
                del self.cursors[key]
                record[1].close()

    def read_range(self, path, revision, offset, length):
        relative_path(path)
        if (type(offset) is not int or type(length) is not int or offset < 0 or offset > 2**63 - 1
                or not 1 <= length <= MAX_RANGE or not isinstance(revision, str) or len(revision) != 64):
            raise NASConnectionError("nas_invalid_request")
        data = self.read_checked(path, revision, offset, length)
        if len(data) > length:
            raise NASConnectionError("nas_budget_exceeded")
        return {"data": base64.b64encode(data).decode()}


class SFTPReader(Reader):
    def __init__(self, config, credentials):
        super().__init__()
        self.client = self.sftp = None
        try:
            import paramiko
        except ImportError:
            raise NASConnectionError("nas_dependency_missing") from None

        class PinnedKey(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                actual = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
                if not hmac.compare_digest(actual, config.host_key_sha256):
                    raise NASConnectionError("nas_host_key_mismatch")

        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(PinnedKey())
        # No system known-hosts, agent, default keys, proxy commands, shell or exec channels.
        try:
            self.client.connect(config.host, port=config.effective_port, **credentials.payload(),
                                allow_agent=False, look_for_keys=False,
                                timeout=config.connect_timeout_seconds, auth_timeout=config.connect_timeout_seconds,
                                banner_timeout=config.connect_timeout_seconds,
                                channel_timeout=config.operation_timeout_seconds)
            self.sftp = self.client.open_sftp()
            self.sftp.get_channel().settimeout(config.operation_timeout_seconds)
            self.health()
        except BaseException:
            self.close()
            raise

    def _path(self, path):
        relative_path(path, allow_root=True)
        full = "/" + path
        # SFTP v3 cannot atomically refuse symlinks at open; the confirmed server root
        # confines races. These checks still reject observed links/nonregular entries.
        current = ""
        for component in path.split("/") if path else []:
            current += "/" + component
            info = self.sftp.lstat(current)
            if statmod.S_ISLNK(info.st_mode):
                raise NASConnectionError("nas_access_denied")
        if self.sftp.normalize(full) != full:
            raise NASConnectionError("nas_access_denied")
        return full

    def _record(self, path, info):
        mode = info.st_mode
        if not (statmod.S_ISREG(mode) or statmod.S_ISDIR(mode)):
            raise NASConnectionError("nas_access_denied")
        if info.st_size is None or info.st_mtime is None:
            raise NASConnectionError("nas_transport_failed")
        kind = "directory" if statmod.S_ISDIR(mode) else "file"
        modified = int(info.st_mtime * 1_000_000_000)
        return {"path": path, "kind": kind, "size": info.st_size, "modified_ns": modified,
                "revision": signature(kind, info.st_size, modified, mode)}

    def health(self):
        self.prune_cursors()
        info = self.sftp.lstat("/")
        if not statmod.S_ISDIR(info.st_mode) or self.sftp.normalize("/") != "/":
            raise NASConnectionError("nas_access_denied")
        # An actual directory READDIR proves root list permission, including empty roots.
        entries = self._iter_dir("/")
        try:
            next(entries, None)
        finally:
            entries.close()
        return {"reachable": True, "authenticated": True, "readable": True}

    def _iter_dir(self, path):
        # Paramiko 4.0 listdir_iter lacks a finally-CLOSE when abandoned midway.
        # Use its pinned SFTP request primitives to keep each remote handle bounded.
        from paramiko import SFTPAttributes
        from paramiko.sftp import CMD_CLOSE, CMD_HANDLE, CMD_NAME, CMD_OPENDIR, CMD_READDIR

        kind, message = self.sftp._request(CMD_OPENDIR, path)
        if kind != CMD_HANDLE:
            raise NASConnectionError("nas_transport_failed")
        handle = message.get_string()
        try:
            while True:
                try:
                    kind, message = self.sftp._request(CMD_READDIR, handle)
                except EOFError:
                    return
                if kind != CMD_NAME:
                    raise NASConnectionError("nas_transport_failed")
                count = message.get_int()
                if not 1 <= count <= 4096:
                    raise NASConnectionError("nas_budget_exceeded")
                for _ in range(count):
                    name, longname = message.get_text(), message.get_text()
                    yield SFTPAttributes._from_msg(message, name, longname)
        finally:
            self.sftp._request(CMD_CLOSE, handle)

    def iter_entries(self, path):
        full = self._path(path)
        entries = self._iter_dir(full)
        try:
            for info in entries:
                name = info.filename
                if name in {".", ".."}:
                    yield None
                    continue
                relative_path(name)
                if "/" in name or not (statmod.S_ISREG(info.st_mode) or statmod.S_ISDIR(info.st_mode)):
                    # Omission cannot certify a complete inventory.
                    raise NASConnectionError("nas_access_denied")
                relative = f"{path}/{name}" if path else name
                yield self._record(relative, info)
        finally:
            entries.close()

    def stat(self, path):
        full = self._path(path)
        return self._record(path, self.sftp.lstat(full))

    def read_checked(self, path, revision, offset, length):
        full = self._path(path)
        before = self.stat(path)
        if before["kind"] != "file" or before["revision"] != revision:
            raise NASConnectionError("nas_revision_changed")
        with self.sftp.open(full, "rb", bufsize=0) as handle:
            if self._record(path, handle.stat())["revision"] != revision:
                raise NASConnectionError("nas_revision_changed")
            handle.seek(offset)
            data = handle.read(length)
            if (self._record(path, handle.stat())["revision"] != revision
                    or self.stat(path)["revision"] != revision):
                raise NASConnectionError("nas_revision_changed")
        return data

    def close(self):
        try:
            self.close_cursors()
        finally:
            try:
                if self.sftp:
                    self.sftp.close()
            finally:
                if self.client:
                    self.client.close()


class SMBReader(Reader):
    def __init__(self, config, credentials):
        super().__init__()
        self.connection = self.session = self.tree = None
        self.root = config.root
        try:
            from smbprotocol.connection import Connection
            from smbprotocol.session import Session
            from smbprotocol.tree import TreeConnect
        except ImportError:
            raise NASConnectionError("nas_dependency_missing") from None
        try:
            self.connection = Connection(uuid.uuid4(), config.host, config.effective_port, require_signing=True)
            self.connection.connect(timeout=config.connect_timeout_seconds)
            self.session = Session(self.connection, **credentials.payload(),
                                   require_encryption=config.require_encryption, auth_protocol="ntlm")
            self.session.connect()
            self.tree = TreeConnect(self.session, f"\\\\{config.host}\\{config.share}")
            self.tree.connect(require_secure_negotiate=True)
            if self.tree.is_dfs_share:
                raise NASConnectionError("nas_access_denied")
            self.health()
        except BaseException:
            self.close()
            raise

    def _open_raw(self, path, directory):
        from smbprotocol.open import (
            CreateDisposition,
            CreateOptions,
            DirectoryAccessMask,
            FileAttributes,
            FilePipePrinterAccessMask,
            ImpersonationLevel,
            Open,
            ShareAccess,
        )

        handle = Open(self.tree, path.replace("/", "\\"))
        access = (DirectoryAccessMask.FILE_LIST_DIRECTORY if directory else FilePipePrinterAccessMask.FILE_READ_DATA)
        options = CreateOptions.FILE_OPEN_REPARSE_POINT
        options |= CreateOptions.FILE_DIRECTORY_FILE if directory else CreateOptions.FILE_NON_DIRECTORY_FILE
        try:
            handle.create(ImpersonationLevel.Impersonation, access | FilePipePrinterAccessMask.FILE_READ_ATTRIBUTES,
                          FileAttributes.FILE_ATTRIBUTE_NORMAL,
                          ShareAccess.FILE_SHARE_READ | ShareAccess.FILE_SHARE_WRITE | ShareAccess.FILE_SHARE_DELETE,
                          CreateDisposition.FILE_OPEN, options)
            if handle.file_attributes & FileAttributes.FILE_ATTRIBUTE_REPARSE_POINT:
                raise NASConnectionError("nas_access_denied")
            return handle
        except BaseException:
            if handle.connected:
                handle.close()
            raise

    def _open(self, path, directory=False):
        relative_path(path, allow_root=True)
        full = "/".join(p for p in (self.root, path) if p)
        components = full.split("/") if full else []
        # Low-level SMB avoids DFS referral resolution and global smbclient caches.
        for i in range(1, len(components)):
            ancestor = self._open_raw("/".join(components[:i]), True)
            ancestor.close()
        return self._open_raw(full, directory)

    def _query(self, handle, info_type):
        from smbprotocol.open import SMB2QueryInfoRequest, SMB2QueryInfoResponse

        value = info_type()
        query = SMB2QueryInfoRequest()
        query["info_type"], query["file_info_class"] = value.INFO_TYPE, value.INFO_CLASS
        query["output_buffer_length"], query["file_id"] = len(value), handle.file_id
        request = self.connection.send(query, sid=self.session.session_id, tid=self.tree.tree_connect_id)
        response = SMB2QueryInfoResponse()
        response.unpack(self.connection.receive(request)["data"].get_value())
        value.unpack(response["buffer"].get_value())
        return value

    def _record(self, path, kind, size, modified, changed, identity):
        return {"path": path, "kind": kind, "size": size, "modified_ns": stamp(modified),
                "revision": signature(kind, size, stamp(modified), stamp(changed), identity)}

    def _stat_handle(self, path, handle):
        from smbprotocol.file_info import (
            FileBasicInformation,
            FileInternalInformation,
            FileStandardInformation,
        )
        from smbprotocol.open import FileAttributes

        basic = self._query(handle, FileBasicInformation)
        standard = self._query(handle, FileStandardInformation)
        internal = self._query(handle, FileInternalInformation)
        if basic["file_attributes"].get_value() & FileAttributes.FILE_ATTRIBUTE_REPARSE_POINT:
            raise NASConnectionError("nas_access_denied")
        kind = "directory" if standard["directory"].get_value() else "file"
        return self._record(path, kind, standard["end_of_file"].get_value(),
                            basic["last_write_time"].get_value(), basic["change_time"].get_value(),
                            internal["index_number"].get_value())

    def health(self):
        self.prune_cursors()
        from smbprotocol.exceptions import NoMoreFiles
        from smbprotocol.open import FileInformationClass

        handle = self._open("", directory=True)
        try:
            try:
                handle.query_directory("*", FileInformationClass.FILE_ID_FULL_DIRECTORY_INFORMATION, max_output=65536)
            except NoMoreFiles:
                pass
        finally:
            handle.close()
        return {"reachable": True, "authenticated": True, "readable": True}

    def iter_entries(self, path):
        from smbprotocol.exceptions import NoMoreFiles
        from smbprotocol.open import FileAttributes, FileInformationClass, QueryDirectoryFlags

        handle = self._open(path, directory=True)
        pending = deque()
        flags = QueryDirectoryFlags.SMB2_RESTART_SCANS
        try:
            while True:
                if not pending:
                    try:
                        batch = handle.query_directory("*", FileInformationClass.FILE_ID_FULL_DIRECTORY_INFORMATION,
                                                       flags=flags, max_output=65536)
                    except NoMoreFiles:
                        return
                    if not batch:
                        return
                    pending.extend(batch)
                    flags = 0
                item = pending.popleft()
                name = item["file_name"].get_value().decode("utf-16-le")
                if name in {".", ".."}:
                    yield None
                    continue
                relative_path(name)
                attributes = item["file_attributes"].get_value()
                if "/" in name or attributes & FileAttributes.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise NASConnectionError("nas_access_denied")
                kind = "directory" if attributes & FileAttributes.FILE_ATTRIBUTE_DIRECTORY else "file"
                relative = f"{path}/{name}" if path else name
                yield self._record(relative, kind, item["end_of_file"].get_value(),
                                   item["last_write_time"].get_value(), item["change_time"].get_value(),
                                   item["file_id"].get_value())
        finally:
            handle.close()

    def stat(self, path):
        # Listed kind is not trusted across reopen; inspect file attributes returned by OPEN.
        from smbprotocol.exceptions import FileIsADirectory

        try:
            handle = self._open(path)
        except FileIsADirectory:
            handle = self._open(path, directory=True)
        try:
            return self._stat_handle(path, handle)
        finally:
            handle.close()

    def read_checked(self, path, revision, offset, length):
        handle = self._open(path)
        try:
            before = self._stat_handle(path, handle)
            if before["kind"] != "file" or before["revision"] != revision:
                raise NASConnectionError("nas_revision_changed")
            remaining = min(length, max(0, before["size"] - offset))
            chunks = []
            while remaining:
                # One credit (64KiB) avoids assuming available multi-credit capacity.
                data = handle.read(offset, min(remaining, 65536))
                if not data:
                    raise NASConnectionError("nas_revision_changed")
                chunks.append(data)
                offset += len(data)
                remaining -= len(data)
            if (self._stat_handle(path, handle)["revision"] != revision
                    or self.stat(path)["revision"] != revision):
                raise NASConnectionError("nas_revision_changed")
            return b"".join(chunks)
        finally:
            handle.close()

    def close(self):
        try:
            self.close_cursors()
        finally:
            # Close the socket/receiver even if SMB LOGOFF would hang.
            if self.connection:
                self.connection.disconnect(close=False)
