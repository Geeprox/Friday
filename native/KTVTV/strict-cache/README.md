# Strict cache bridge (offline candidate)

This directory implements a complete local packet server, header-only C client,
lease state machine and adapter to the inspected GlobalMic cache. It has no
AudioRecord/AAudio input, ALSA open, USB fallback, PCM logging or recording.
It supports the legacy observer plus separate wet return (capabilities31) and
the exact-build inline effect module below (capabilities63).
An ordinary APK **cannot deploy or create this bridge**.
Missing platform integration, config, identity or patch proof must leave that
mode unavailable; do not claim that source compilation is deployment.

## Inline system module (main0.60 / audio0.5, offline candidate)

`scripts/build_ktv_system_audio.py --vendor-source <exact-private-original> --output <new-private-directory>`
builds two firmware inputs without running vendor code or contacting a device:
the original fixed HAL module name as a TMIS shim and its private
`libfridaygm.payload.so`. The shim publishes the full11-slot table only after
the payload, relocated bindings and local bridge have passed verification.
The existing HAL loader can reach its ordinary TMIS entry point; no injected
constructor inside an app or guessed service/SELinux permission is substituted.
Unknown firmware or failed initialization returns0 (the vendor caller treats
any nonzero result as success), never a partial table or negative errno.

The derived payload changes only the verified silent VolUnit instruction,
private SONAME and DT_FLAGS to retain BIND_NOW and add DF_SYMBOLIC. Full file/RX
hashes and every supported self relocation are verified; the actual mapped TMIS,
method table and exports must belong to that same payload. A fixed pre-worker
init slot installs an object-local EffectUnit vtable. Its owned stereo48k PCM16
callback applies `InlineVoiceKernel` to the original post-volume block and
forwards that same pointer/byte count to the original PcmWriteUnit. There is no
extra dry FIFO, second dry gain, second capture, OpenSL output or FFT-window wait
on this path. Two fixed GOT slots are restored on shutdown; executable bytes
are never rewritten in memory. Unowned SDK sessions keep their original effect
callback. DSP storage survives until the original payload has stopped/joined.

Reader-only INLINE_CONFIGURE(8) carries bounded preset/wet/reference-gain/mute
scalars; INLINE_STATUS(9) returns the fixed56-byte metrics body and no PCM.
Only the current owner may send INLINE_MUTE(10) or INLINE_NOTCH(11). The owner
gate starts closed and reader configuration cannot open it. Opening requires
2048 successfully read observer frames, a read within250ms and an actual bound
native output transfer completion within250ms. A separate fixed transferData
slot observes only the exact owned Mtk output caller/Alsa object; input or
foreign calls do not count. First completion has a250ms closed-gate grace.
Negative output results or stalled completion latch the gate closed until a
stopped session is explicitly prepared again. A completed call does not prove
successful byte count, speaker sound or acoustic latency.

INLINE_STATUS flags are Installed1, Prepared2, Bound4, Owned8, Muted16,
Bypass32, Live64, NotchPending128 and OutputReady256. A live bound inline stream
requires mask77, plus OutputReady before UI readiness or positive gain. The
56-byte body and normal576-byte maximum packet are unchanged. Existing31-bit
capability clients keep the original observer behavior; clients refuse other
capability combinations. Reader disconnect immediately closes the inline gate;
owner expiry also retains the finite original-gain safety-zero fallback.
An authenticated inline operation whose Source returns ESTALE revokes that
binding immediately, including configure/status/mute/notch; it does not wait for
the next reader request or owner heartbeat. An old/unauthorized request or an
EAGAIN busy trylock does not revoke a current healthy owner.

This build is a private firmware integration input, not a root installer or an
OTA. Keep the exact original firmware/module recovery and a supported platform
update/recovery channel before integration. Configure actual package UIDs with
the private file rules below; prove system UID1041, vendor linker visibility,
SELinux access, same control-instance cookie and actual native I/O on site.
Neither the APK pair nor offline ELF emulation establishes those runtime gates.
No technical channel chooser is exposed in the product. Physical latency,
feedback suppression/false positives and reverb quality remain separate checks.

## Legacy observer host lifecycle

The server DSO is only for an authorized platform/HAL integration inside the
existing audioserver UID **1041** process. Exported lifecycle functions are
`int friday_strict_cache_start(void)` and `void friday_strict_cache_stop(void)`.
A constructor attempts start; failure starts no listener. Explicit start can be
called after GlobalMic is already loaded. Stop wakes/joins the server thread,
closes every socket and drops its loaded-image reference. It has no vendor
START/STOP or positive-gain operation. After a successful owned BIND only, expiry
or owner release can request the one-way safety zero described below. No
installation or loading command is provided.

## Integration API

Include `StrictCacheClient.h` from C11 or C++. Initialize with
`FridayCacheClient client = friday_cache_client_init();` and serialize all calls
for one client on its owner worker. `connect` is for a new/disconnected client;
close an earlier client before reconnecting. Every timeout is 1..250 milliseconds
with one monotonic deadline covering connect/auth/HELLO or a whole RPC.

```c
friday_cache_connect(&owner, FRIDAY_CACHE_OWNER, 80);
friday_cache_begin(&owner, lease, alsa_card, alsa_device, &generation, 80);
/* The dry owner performs its separate START and completed ZERO operations. */
friday_cache_bind_reader(&owner, 0, 80);
/* BIND checks the actual source, drops old cache once, and advances generation. */
friday_cache_connect(&reader, FRIDAY_CACHE_READER, 10);
friday_cache_attach(&reader, lease, 10);
friday_cache_read128(&reader, pcm16_stereo_256_samples, &sequence, 10);
friday_cache_heartbeat(&owner, 80); /* owner sends at least every 200ms */
friday_cache_end(&owner, 0);       /* best-effort nonblock send + immediate close */
friday_cache_close(&reader);
```

Zero is success. Negative errno values include `-EAGAIN` (busy/empty/discarded
backlog), `-ESTALE` (binding/source/lease no longer current), `-EBUSY` (another
owner/reader), `-EACCES`, `-EPROTO`, and `-ETIMEDOUT`. Failed reads always clear all
256 destination samples and return no sequence. Successful reads contain exactly
128 stereo PCM16 frames at 48000Hz. PCM and temporary buffers are explicitly
cleared. End and close never wait for a response. RPC timeout closes the socket;
a late response cannot contaminate the next RPC. Callers must not spin on EAGAIN
or silently select another capture backend.
Only BIND/ATTACH initialization metadata retries a busy trylock, with1ms pauses
inside that call's original deadline. READ and steady PCM handling never retry
through this initialization path. An authenticated current-owner heartbeat
renews liveness even if the source trylock is busy; it emits no PCM frames and
does not prove input readiness. Actual source mismatch still revokes ownership.

HELLO is read-only and does not reserve ownership or alter another connection.
Metadata is available as `server_pid`, `server_uid`, `capabilities`, `rate`,
`channels`, `format` (1 = PCM16), `instance_cookie`. Kernel SO_PEERCRED must report UID1041 and the
same positive PID carried by each response. Capabilities must be all five bits:
SILENT_START=1, CACHE_ONLY=2, MEMORY_ONLY=4, BOUNDED_READ=8,
VERIFIED_MAPPED_IMAGE=16. These describe verified implementation capabilities;
they do not mean a microphone is running or an acoustic result is accepted.

## Wire and ownership

The only endpoint is abstract AF_UNIX SOCK_SEQPACKET
`friday.ktv.strict-cache.v1`; no network or caller-supplied path exists.
`StrictCacheProtocol.h` is the exact LE ABI: 64-byte request and 64-byte response;
only a successful READ adds 512 PCM bytes. Inline STATUS adds its56-byte scalar
body. Other packets, including HELLO and errors, contain no PCM.
Malformed/truncated packets are refused, not reassembled.
There are at most eight peers, with two connections per configured UID. One
packet per ready peer and a nonblocking send prevent a stalled peer holding the
server loop. A full response queue closes that peer; owner close revokes readers.

BEGIN reserves a lease even before the dry source is running. BIND checks and
captures the current cache identity. With reader_pid=0, the first ATTACH from the
configured main UID pins that PID; a second active reader is refused. Positive
reader_pid binds only that PID. BEGIN/BIND advance generation; old commands do
not revoke a newer owner. Owner identity includes the connection ID, not just
PID: a HELLO probe in the same Guard process can close without revoking the owner.
Heartbeat loss of 750ms, owner disconnect/END or a mismatched/stopped source
revokes the binding. Expiry is checked on every operation and at most 10ms apart
when idle. No automatic rearm or fallback occurs.

## Mandatory platform gates

`/data/vendor/friday-ktv` must be a real directory owned by root/1041, mode0700.
`cache-clients.conf` must be a real single-link file owned by root/1041, mode0600.
Both are opened with NOFOLLOW. The bounded config accepts only explicit decimal
`owner_uid` and `reader_uid`, each an application UID >=10000. The example has
placeholders and cannot authorize anyone until locally configured. Determine
package identities on site; never use an author's example UID as a household
default. The DSO must also be permitted by the device's actual SELinux domain
and vendor linker namespace; this repository has not established that permission.

The host enumerates already loaded modules, accepts exactly one basename
`libtlkg.hal.globalmic.so` under `/vendor/` or `/system/`, and uses that **observed**
path for RTLD_NOLOAD. No guessed alternate path, fresh load or manager creation is
attempted. After pinning its handle, it re-enumerates and hashes the entire actual
RX PT_LOAD: offset/vaddr0, flagsRX, exactly614408 bytes. Required mapped SHA256:
`618a2f2d4a60b17d0d3e52167d564f8064a2129314a97efffbce5273f30c4ef8`.
It separately checks the silent-start `fmov s3,wzr` at RVA0x39a54 and verifies
each used export/vtable address against its exact RVA. This requires the parent's
zero-initialization patch; checking an installed file alone cannot replace the
mapped code check. The mapped RX has no relocation write destinations in the
parent's saved static validation. No vendor binary is shipped by this directory.

Manager is image base+0x9dec0, strategy is manager+0x2e8. The adapter requires
Strategy/Single/CacheUnit vtables matching the pinned image and the current
running state, card/device, stereo and48000Hz. It captures the exact cache and
loop-buffer pointers at BIND and rechecks those identities on reads. This version
supports the inspected **Single** implementation only; Shared is refused.
Strategy.stop takes its MMutex+0x20 before calling Single.stop, which takes
MMutex+0x48; MMutex's pthread mutex is this+8. Adapter therefore uses only
`pthread_mutex_trylock(strategy+0x28)` then `trylock(single+0x50)`, with reverse
unlock. The two vendor calls are only `loopbuffer_len` and
`CacheUnit::getData`; no pcmDevOpen, syncReadDryPcm, tryStart, proxy_read/open or
capture constructors occur. BIND discards at most10240 cache bytes once; empty
get enables cache production. A READ with more than1024 queued bytes drops that
bounded backlog once and returns EAGAIN. It does not forward dry PCM to output,
change dry gain during ordinary reads, or consume the separate dry playback path.

## Same control-instance cookie

UID1041 plus an exact mapped code hash do not prove that AudioManager control
commands reach that same GlobalMic instance. Before opening its listener, the
host now verifies the version-pointer slot at image RVA0x9e1d8 belongs to this
image's RW PT_LOAD and an actual private rw/non-executable `/proc/self/maps`
mapping. Its original pointer must be inside the fully SHA-verified RX image
and contain exactly `1.1.3\0`. A CAS then publishes
`1.1.3+friday.<16 lowercase hex digits>` using a random nonzero63-bit cookie.
Unknown/foreign version pointers or failed CAS prevent the server from opening.
No vendor file or original rodata bytes are changed.

HELLO's response.sequence carries that cookie, with generation/lease still0 and
no PCM. The client records it only in `instance_cookie`, never in the READ
`sequence` counter. Main/Guard integration must compare it with the strict raw
version from the original AudioManager `getParameters("61961")` route before
any START. A missing, different or old-server cookie must refuse the mode.
Before BEGIN, an owner reconnect must also match the previously verified
expected cookie. This Java/JNI policy is supplied by the parent integration;
the bridge/client do not falsely claim framework-route confirmation themselves.

Stop restores the original version pointer only with a CAS that still finds
this host's published pointer. A successful publication's30-byte malloc string
is intentionally retained for the process lifetime, including after stop/DSO
unload: a vendor getter may have borrowed it just before restoration. Another
host's replacement is never overwritten. Each successful fresh start creates
a fresh cookie and retains at most another30 bytes of non-PCM metadata.

## Owned-expiry safety zero

After a successful BIND, owner disconnect, END, expiry, source failure or explicit
rebinding retires that particular owned binding. On the maintenance thread only,
the adapter first tries the same two locks and rechecks all captured object
identities, current card/device, running state and format. Single+0x38 must still
be the captured VolUnit with the exact mapped-image vtable. Only then are its
two inspected aligned 32-bit float multipliers at+0x1048/+0x104c atomically set
to zero. No arbitrary address/value or setter request exists in the client
protocol. No probe or never-bound session can cause those writes. Identity
change refuses the write, so an old lease cannot touch a replacement route.

Lock contention schedules at most ten trylock attempts within100ms, spaced at
least10ms by the existing host maintenance loop. It never waits on a vendor
mutex. The same maintenance thread performs the finite shutdown retry window;
clock failure or exhaustion records a fixed failure in in-memory Safety
counters. A successful new BIND cancels old pending work before it can land in
the new lease. Safety counters distinguish attempted stores, completed zero
stores, exhausted busy locks and refused identity; they contain no PCM and are
not a claim of actual physical mute. The wire protocol does not manufacture a
mute-confirmed acknowledgement. This best-effort owned safety action cannot
guarantee silence after a permanent vendor lock failure, replaced channel or
hardware fault, and does not add any automatic unmute path.

These bounds concern the observer cache and software transport. They do **not**
measure physical capture age, acoustic round-trip latency, feedback accuracy or
reverb quality. Actual module hosting, SELinux, peer permissions, source flow,
dry/wet coexistence and acoustic acceptance remain separate gates.

## Offline verification

Run `python3 native/KTVTV/strict-cache/verify_offline.py`. It compiles/runs only
synthetic memory/socket tests under ASan+UBSan, cross-compiles the platform DSO
for Android arm64 using the existing pinned NDK, and syntax-checks the C client
for arm64/armv7. It never loads a vendor DSO or touches a device, NAS, microphone,
audio output or model. Receipts and binaries remain under ignored `.artifacts/`.
macOS has no AF_UNIX SEQPACKET: packet tests use DGRAM there, with a stream pair
testing backpressure/deadline behavior. Android SO_PEERCRED and the actual host
are cross-compiled, not executed by these tests.
