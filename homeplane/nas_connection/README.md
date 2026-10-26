# Direct NAS connection owner (F1)

This module owns a persistent direct SMB2/3 or SFTP connection in a supervised
worker. It has no Finder/mount, camera, media decoder, model, HA, device-action,
generic SSH command, SSH agent or NAS write dependency. Construction and status
reads do not connect. Configuration defaults to disabled. The worker exposes only
connect, health, list, stat and bounded range reads.

## Consumer seam

```python
from homeplane.nas_connection import (
    NASConnectionConfig, NASConnectionError, NASConnectionManager, NASCredentials,
)

async def resolve(reference: str) -> NASCredentials | None:
    # Integration supplies an approved private credential store; no ambient fallback.
    return None

manager = NASConnectionManager(local_state_path, credential_resolver=resolve)
await manager.start()       # honors durable enabled/paused intent; no automatic enable
status = manager.status()   # synchronous, cached, path-free; never refreshes/authenticates
page = await manager.list_page(source_ref, cursor=None, limit=128, directory_ref=None)
entry = await manager.stat(source_ref, object_ref, expected_revision=None)
data = await manager.read_range(source_ref, object_ref, expected_revision, offset, length)
await manager.disable(expected_generation=status.config_generation)
await manager.close()       # service teardown retains enabled/disabled intent
```

Configuration is a frozen `NASConnectionConfig`; `configure(config,
expected_generation=N)` atomically saves a new generation and starts only if
`enabled=True`. `resume(expected_generation=N)` is the explicit enable/retry
operation. An exclusive local owner lock prevents competing managers for the same
state file. State is local, atomically replaced and fsynced; it contains endpoint
configuration and a credential reference, never username/password, file maps,
media bytes, live status or work requests. Authentication/access/host-key/dependency
failures persist a pause until explicit resume or configuration change.

`NASConnectionStatus` separates configured, reachable, authenticated and readable.
Only `state="online"` and all three latter fields `True` admit reads. Unknown is
`None`. Status expires conservatively after the heartbeat interval plus operation
deadline. Restart clears health evidence. Fresh connection epochs fence every
result; changed configuration, stop, cancellation or reconnect invalidates old
object refs/cursors. Reads fail once and are never automatically replayed. The
background owner reconnects with bounded exponential backoff; it checks directory
permission on heartbeat without reading file content.

Entries expose opaque `object_ref`, `kind`, metadata `revision`, `size`, nullable
`modified_ns`, extension-derived `media_kind`, `closed=None` and
`revision_basis="metadata"`. Paths live only in a private in-memory map (8,192
objects maximum). Directories can be passed by `directory_ref` for bounded
traversal. At most eight cursor handles remain open; cursors expire after 120
seconds, pages contain at most 128 entries and one traversal visits at most 4,096
entries. Re-list after reconnect/restart; do not persist cursor or object refs as
permanent media identity.

Pages expose generation, epoch, next cursor, enumeration completion and an opaque
completion token. **`snapshot_consistent=False` always.** Neither client creates an
atomic directory snapshot. Completion proves only that this bounded enumeration
reached EOF under its connection fence. Absence does not prove deletion. Explicit
stat `nas_missing` is required to mark a known source object missing. A token does
not prove file closure, camera identity, recording time, immutable content or
playability. `media_kind` is a filename-extension hint, not decoded MIME evidence.

Ranges are at most 1 MiB. Each fresh read opens read-only and checks metadata before
and after on the same handle and on the reopened pathname. SMB includes file ID,
size, modification and change times (microsecond normalized); SFTP v3 has coarse
size/mtime/mode metadata and no reliable inode. Neither detects same-metadata
substitution, and separate ranges do not lease an immutable full-file snapshot.
A consumer may compute bounded SHA256, but must retain these evidence limitations,
review file-completion conventions, and discard incremental hash state on epoch or
generation change. No whole-file copy or full-stream hash is retained here.

Errors are `NASConnectionError.code` from the fixed contract set; arbitrary upstream
exception text is discarded. Consumer-relevant codes: `nas_missing`,
`nas_revision_changed`, `nas_source_changed`, `nas_object_unknown` (re-list),
`nas_cursor_expired`, `nas_unavailable`, `nas_disabled`, `nas_busy`,
`nas_budget_exceeded`, `nas_timeout`. Cancellation propagates `CancelledError` and
drops/reaps the worker. A blocked native call is first given 0.2 s for EOF cleanup,
then 0.2 s for terminate, then killed/reaped; no queued read survives this event.

## Transport choices against the existing project

The captured project supports only `smb://` to an existing mount or an absolute
mounted directory. `SettingsService.probe_nas` and `ArchiveIndex` read `/sbin/mount`
and use local no-follow workers. No SMB/SFTP Python client is installed in its
original environment. Those protected integrations are unchanged by F1.

SMB uses pinned `smbprotocol==1.15.0` low-level Connection/Session/TreeConnect/Open,
mandatory signing, encryption by default, explicit username/password, FILE_OPEN,
read-only access masks and OPEN_REPARSE_POINT. It rejects DFS shares and observed
reparse points and does not use global smbclient caches, referrals or mount paths.
The configured share is the server scope; server share/account confinement is still
required. Intermediate directory checks are not an atomic open-beneath primitive;
server-side symlink/reparse changes must not grant access outside the approved share.
No local filesystem lock is used to interfere with NAS recording writers.

SFTP uses pinned `paramiko==5.1.0` with an exact SHA256 host-key fingerprint and
password-only authentication. It opens only the SFTP subsystem. No key discovery,
agent, proxy, shell or exec is enabled. SFTP v3 lacks atomic open-beneath/no-follow:
configuration therefore requires `root="/"` and explicit
`sftp_root_confined=True`, representing an independently confirmed server-confined
account root. This module does not create that account or change NAS permissions.
Observed symlinks/nonregular files are rejected. Its bounded directory generator
uses pinned Paramiko request primitives with finally-CLOSE, because 4.0's
`listdir_iter` leaves abandoned directory handles open.

Primary API sources used for comparison: [smbprotocol Open source](https://github.com/jborean93/smbprotocol/blob/v1.15.0/src/smbprotocol/open.py),
[Paramiko client](https://docs.paramiko.org/en/stable/api/client.html),
[Paramiko SFTP](https://docs.paramiko.org/en/stable/api/sftp.html).

Install `requirements.txt` into the selected Friday runtime or adopt the separate
optional-dependency proposal. A missing dependency pauses clearly; installation
does not enable the connection. The manager needs an approved async credential
resolver, an explicit endpoint/share/root config, and native service lifecycle
wiring. No real NAS credentials, transport capability, throughput, file layout,
completion convention, clock, playback, outage or recording noninterference were
qualified here. No LAN throughput, wired link or credentials are inferred.

## Synthetic validation

`tests/test_homeplane_nas_connection.py` covers durable disabled/auth-paused state,
owner locking/CAS, fresh epochs, explicit missing and changed revisions, bounded
backoff/deadlines, stale status, no queued/replayed reads, path/ref budgets, source
change races, actual hung subprocess kill/reap, fixed redacted IPC, actual SMB
library structures against a fake protocol endpoint, and encrypted loopback SFTP
with generated bytes/test credentials. The supervised SFTP-worker test also checks
credential-free argv/environment and graceful reaping.

Install the pinned optional dependencies for transport tests; set
`FRIDAY_NAS_TEST_PYTHON` to that runtime to exercise the actual restricted worker.
Loopback test binding may need the execution sandbox's network allowance. All file
content in these tests is generated synthetic material. Production live checks are
outside this checkpoint.
