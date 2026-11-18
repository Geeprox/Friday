# NAS KTV coordinator (protocol 2)

The NAS owns room/session state, guest queues, commands, public search and TV playback receipts. The Mac prepares songs and exposes only signed, bounded media reads to the TV/NAS. Phone requests and TV controls no longer require a Mac relay in direct mode. This changes coordination, not the real-time microphone DSP or acoustic latency.

`nas_server.py`, `coordinator.py`, `audio_format.py`, `management.py`, `tv_updates.py`, `tv_update_inbox.py`, `supervisor.py`, `launch.py` and `web/` form the standalone Python 3.8+ service; there are no third-party NAS Python dependencies. Configuration, keys, SQLite, cached media and supervisor receipts remain in a private directory outside `web/`.

NAS 的 `/v1/status` 可声明 `audio_formats`。电视确认该字段后，在就绪上报中声明可解码格式。只有新鲜的 AAC 就绪记录会让 Worker 领取 `playback_encoding: aac_lc_m4a` 任务。旧电视、旧 Worker 和未知能力继续走 WAV。压缩媒体使用既有摘要、票据、原子归档和请求范围校验，双轨的帧数与码率必须一致。`audio_format.py` 在 NAS 验证 M4A 容器、AAC LC 参数及显式裁切时间线，服务不启动编码器或音频设备。

## Startup and readiness

Only an authenticated TV `POST /v1/party` with `instance` and `request_id` UUIDs opens/adopts a session and arms preparation/recovery. A phone preview, read-only status poll, NAS timer or worker registration cannot open a session or wake a TV. The TV renews its existing instance using `POST /v1/tv/readiness` every few seconds; that lease survives NAS restart without creating a new session.

All TV endpoints require a separate `X-KTV-TV-Key`, stored locally in `state/tv-key`. They do not accept the Mac agent key. Mutating TV reports also require `X-KTV-TV-Instance`. The Mac uses the existing agent bearer key. No keys appear in URLs or diagnostics.

`GET /v1/status` returns protocol 2, current `party`, and `readiness`. The latter is complete only when all fixed gates are true: `nas_api`, `nas_data`, `nas_media`, `qr`, `link_worker`, `link_tv`, `worker_source`, `worker_model`, `worker_download`, `worker_lyrics`, `worker_media`, `tv_playback`, `tv_audio`, `tv_media`. Worker claims expire after 20 seconds, TV receipts after 12 seconds, and NAS local health evidence after 15 seconds. The NAS filesystem/SQLite read-write probes run on a separate health lane; request handlers only read their bounded snapshot. First startup waits for those results. These are software/capability receipts, not proof of physical sound or microphone feedback protection.

新版电视收到开房回执后即可显示房间。电视的就绪上报、二维码和歌曲列表分别处理，任一路暂时超时，其余状态仍可更新。Mac Worker 从状态轮询交付当前二维码，避免等待媒体归档、歌词或伴奏任务。暂时失败时，前台电视用原请求 ID 继续以最长 10 秒的退避间隔重试。明确拒绝、密钥错误和房间冲突等待用户处理。

启用后，电视开房会在第 0、2、6、14 秒访问已配置的 Mac 固定 `/v1/status` 监听器。单次超时为 1 秒，首轮窗口为 30 秒。前台电视继续有效上报、Worker 一直没有新鲜注册时，同一个开房请求每 60 秒最多补一次探测。仍在线的 Worker 如果只是依赖未就绪，等待依赖状态更新。收到 401 表示监听器已响应，完整就绪仍取决于新鲜的 Worker 注册与依赖检查。接口仅联系固定监听器，`cannot_wake` 表明它不负责从休眠或断电唤醒 Mac。

The local NAS supervisor uses 2/4/8/16-second backoff and stops after five short-lived children. It writes `restart-blocked.json`; scheduled `launch.py --ensure` respects that hold, while an explicit operator start can clear it. It never kills unrelated processes.

The Mac's existing launchd service also has a persistent outer `serve` startup budget: five admissions per ten minutes with 0/1/2/4/8-second delays. Its private `startup-budget.json` sits beside `--public-config` (or the private `--config` in standalone mode), uses atomic 0600 writes and a file lock, and fails closed if it cannot be safely stored. Dependency imports and service initialization are inside this admission guard. Only an actually started HTTP server arms the 60-second stability timer; old/cancelled timers cannot reset a newer attempt or configuration. Exhaustion returns a successful exit so `KeepAlive.SuccessfulExit=false` stops crash retries. The blocked socket-activation path also boundedly accepts/closes already queued connections on the exact verified inherited listeners, including abandoned requests, without reading requests or starting a worker. It does not change the plist or create listeners. A later cooldown, private configuration/critical source change, or an explicit local `python scripts/ktv_server.py --reset-startup-budget --config data/ktv/config.json --public-config data/ktv/nas.json` can rearm the budget. This reset alone does not start the service and has no NAS/HTTP command equivalent. `init`, `check`, help and other non-serve actions are not counted. Ordinary launchd sockets, listener identity checks and service ownership stay unchanged.

## Listeners and transport

Mac 制备 Worker 的私有 `--public-config` 在 `coordination: "nas"` 模式下可设置 `lan_ipv4`。填入 NAS 现有 HTTPS 反向代理的 RFC1918 IPv4，只改变 Mac 连接该 NAS 的 TCP 地址。沿用现有 `endpoint` 的 HTTPS 域名、端口、路径、`Host` 和 TLS SNI，并继续验证证书。省略 `lan_ipv4` 时沿用原公网连接方式。手机二维码与电视的局域网媒体地址继续使用各自配置。

The default listener remains loopback. An optional explicit `lan_host` RFC1918 IPv4 adds one listener on the same configured port and the same Store; the HTTPS phone proxy continues using loopback. Wildcard, public, link-local, DNS and loopback values are refused for `lan_host`. A partial bind failure closes already-created listeners. The service does not alter DHCP, DNS, routing or firewall settings.

`GET /api/rooms/{id}/events?after=<cursor>&wait=5` provides authenticated, bounded long polling as `{cursor,status}`. There are at most eight waiters, leaving normal request capacity for controls. Updates wake listeners; an unknown or larger old cursor returns a current snapshot immediately. Event responses are not midpoint clock probes. All control success still depends on the TV's actual applied revision and state.

### 通过既有 HTTPS 9443 接入外网访客

将 [访客路由模板](nginx-guest-9443.locations.conf.example) 保存为 `.inc`，由已有 TLS `server` 显式包含，使用实际 NAS KTV loopback 端口。模板放行访客首页、健康检查、明确列出的前端文件与房间操作。电视及 Worker 使用原私有服务地址。沿用 9443 的证书和 Host 校验，保留同站门户。检查 Nginx 配置、访客健康检查以及原门户，再切换二维码地址。

- NAS 私有配置的 `public_origin` 设置为访客 HTTPS 来源，例如 `https://guests.example.test:9443`。已加入的访客仍需使用原地址时，可将原 HTTPS 来源写入 `previous_guest_origin`。这项兼容仅适用于房间访客接口。
- Mac Worker 私有配置的 `public_join_endpoint` 设置为相同来源和现有路径，例如 `https://guests.example.test:9443/ktv`。Worker 继续按原 `endpoint` 和局域网 TLS 配置连接管理接口。
- 公开来源改变后，NAS 会等待按新地址生成的二维码，房间及邀请凭据保持有效。核对新二维码确实使用 9443，再从外部网络打开访客页面。只有 AAAA 记录的入口需要访客网络支持 IPv6。
- 点歌页用 `GET /?room=current` 获取当前邀请，以便纯链接和旧二维码直接入场；这个请求沿用现有访客首页的代理路径和浏览器来源。服务也接受等价的 `GET /api/rooms/current`。
- 同机容器网关可用私有配置 `guest_socket` 连接 NAS 访客接口。路径相对于 NAS 配置目录，父目录由 NAS 服务账户持有，权限为 0710，套接字为 0660。网关以该目录的组连接，仍按访客路径转发；套接字只提供页面、健康与房间访客接口。原有电视和 Worker 保持原局域网路由。

## 电视应用更新

电视使用自己的 `X-KTV-TV-Key` 从固定内网地址读取 `GET /v1/tv/update`。结果为 `{version_code,version_name,bytes,sha256,url}`，其中 `url` 固定为 `/v1/tv/update.apk`。APK 接口支持 GET、HEAD 和单段字节范围。带摘要的 `If-Match` 在发布刚切换时返回 412，电视可重新检查。尚未登记或当前成品失去校验时返回 404。访客的 9443 白名单不包含 `/v1`。

发布文件位于配置旁的私有 `state/tv-updates/`，与页面和媒体数据独立。Mac 发布工具沿用电视安装预检，核对主应用与伴随包的实际 Android 签名、构建回执、源码和字节，主应用限 64 MiB。Mac 通过私有挂载将已核对的 APK 与认证记录原子投递至 NAS 创建的单件收件目录，普通 NAS 服务账户在后台以本机 0700／0600 权限登记。认证使用现有 Worker 密钥和独立用途标识。投递有效期为 15 分钟，Mac 命令收到匹配版本、字节和摘要的认证回执后才报告已发布。此过程不依赖电视在线或房间开启。

发布锁串行处理变更，先登记摘要和完成文件，再原子切换当前版本。版本号只能增加，后续发布沿用首个发布版本证书。上一版保留供维护核对，接口只提供当前版本。发布中断后可重试同一版本，发布期间可继续读取已打开的成品。挂载端的模式显示可能由 SMB 合成，排查异常时由现有 NAS 服务账户在本机运行 `diagnose_tv_update.py`，用已验签发布回执的 `version_code`、`bytes` 和 `sha256` 检查实际状态。

## Sessions and stale actions

The household identity is persistent; each explicitly ended session has its own room/queue identity. Existing active rooms and their songs/guest identities migrate without deletion. Empty queues, warm-up, pause, worker failure and temporary disconnection do not close a session. The compatibility `expires` value is not a session timeout when `persistent:true`; only explicit close ends the session. Retention cleanup applies to explicitly closed sessions after its independent horizon.

Legacy-room adoption is a one-time household initialization step. A separate persistent household marker survives scene closure, 24-hour metadata cleanup and NAS restart. Before the first TV opening, a restart still permits adopting the latest legacy room. After initialization, explicitly ending and reopening always creates an empty new scene; another unselected old room is preserved rather than silently reused. The household identity stays the same.

While a direct TV session exists, preparation task leases are restricted to that current room. Other preserved legacy rooms and their queues remain intact and are not silently sent ahead of the current queue. After an explicitly closed session passes the 24-hour retention horizon, its song/guest/retry/command records, manifest and publication history, room-token history, start-request associations and obsolete coordinator-session pointer are cleaned together. Active sessions are excluded. Content-addressed media and its verification proof have a separate lifetime.

Joined guest credentials currently remain valid until the session is explicitly ended. There is no new independent guest time TTL. When a QR key changes during adoption, a guest who already joined can still use the prior room key together with that guest credential; an old QR alone cannot admit a new visitor. No existing guest is revoked by this migration.

手机用一次 `POST /?room=current` 取得当前邀请、设备身份及歌单快照；原 GET 与逐房间加入接口继续兼容。浏览器在同一站点保存随机设备凭据及 Cookie 备份，NAS 只保存摘要。凭据按房间导出访客身份，迁移时可恢复原 v1 访客；新的场次沿用设备显示资料。旧凭据仍有效，重复加入不创建第二个人。头像编号 0–11 保持原映射，12–31 为新角色，电视 0.78 动态读取同一图集。

NAS 在实际电视播放回报中按设备摘要幂等更新常唱记录，手机不必持续在线。`GET /api/rooms/{id}/profile?frequent=1` 只返回当前设备自己的曲库及云盘常唱。云盘目录可附带专辑及官方图片地址，显示资料随完整快照一起写入 `song_catalog`；残缺分页保持旧资料。缺少资料时再按歌名和歌手核对公开录音，已点与管理清单复用；同数值的曲库 ID 和云盘 ID 分开存放。

首屏入口合并基础脚本，通过版本化资源缓存及 gzip 减少重复传输。歌曲查询合并同查询的并发请求，曲库首次请求缓存相邻两页。封面在可见行附近独立补齐，暂时失败只影响对应图片；设置预览在打开设置时才加载。`artwork` 接口除 `covers` 外可返回 `tracks` 与 `albums`，管理接口沿用相同资料。

手机加入后即可使用「常唱歌手」。NAS 先返回已保存的推荐歌单，并在后台更新上游数据。首次加载时，手机会自动刷新歌单，搜索和播放控制可以照常使用。

打开「已点」后，按住歌曲右侧的排序手柄可连续拖过多首歌曲。歌曲的「⋯」菜单提供「置顶」，将这首移到待唱歌单最前面，当前演唱继续。暂时没有同步歌词的版本可以通过「找其他版本」回到曲库搜索。[查看歌曲操作示例](../../docs/screenshots/ktv-mobile-queue.png)。

「歌曲声音」会立即显示选择的原唱或伴奏。电视确认较慢时，会在音轨选择下方提示，仍可继续切换。「歌曲声音」和「氛围」使用同样的标题、关闭按钮和音量滑块。手机初次扫码遇到短暂断线会在页面可见时继续重连。[查看声音面板示例](../../docs/screenshots/ktv-mobile-sound.png)。

Start request IDs are durable and idempotent throughout the retained session window. A delayed old request cannot reopen a retained closed session or reclaim a newer TV instance. Closing requires both the current TV instance and current `room_id`. New TV instances skip old command cursors and start with microphone mute requested. Legacy Mac bridge playback/control/close writes are refused for a directly owned room.

An unmute gesture is accepted only with a fresh direct-TV lease. Its pending intent expires after five seconds unless the TV reports `mic_muted:false`, actual `voice_protection.mute_confirmed:true` and an applied revision at least as new as that gesture. Expiry raises a new mute revision; network recovery cannot revive the old enable request. Already confirmed live microphone state is not muted merely because the gesture is old. Older clients without the confirmation capability retain the legacy receipt interpretation; direct mode uses the new TV pair.

## Finished media and parallel archive

`POST /agent/worker` registers `{instance,origin,capabilities}`. Capabilities are exactly the five worker gates above without the `worker_` prefix. The private HTTP IPv4 origin is configured or pinned on first authenticated registration and cannot silently change.

`POST /agent/manifests` publishes `{id,instance,version,timeline,job,lyrics,media_ticket,media_expires}` with optional `guide`. `id` is the NAS queue UUID, `version` is a 64-hex immutable publication identity, and `timeline` is `{sample_rate:48000,frames,duration_ms}`. Original/instrumental media are SHA256/byte-count/timeline bound; video retains its bounded edition/alignment evidence. A previously superseded publication cannot be replayed. A ready update with no valid manifest is not advertised as TV-ready media.

Each descriptor in `/v1/jobs` has `sources`: a verified NAS cache first when available, followed by the pinned Mac's fixed `/v1/jobs/{MacUUID}/media/{kind}` with the short-lived `X-KTV-Media` ticket. Only media permission crosses to the TV; no Mac management/account key does. Expired direct tickets or unavailable file service are withheld. Source/model failures degrade full readiness while still allowing already finished media from a fresh, working Mac file service.

NAS caching uses independent bounded workers. First TV playback can use Mac direct delivery without waiting for NAS bytes. NAS cache publication uses temporary files, SHA/size/format checks, fsync and atomic replacement. Archive state is independently `pending/ready/failed`; `/agent/tasks.archives` is the Mac's actual archive acknowledgment. A manifest acknowledgment is not one. Cache failure leaves the direct alternative usable.

Already-started archival can finish after a scene is ended or the publication gains guide/lyric metadata, provided the retained current manifest still references that kind's exact SHA and byte count. The worker can also use authenticated `POST /agent/archive-check` with at most 16 `{id,files:[{sha256,bytes}]}` items and at most five files per item. It returns content proof independently of room activity or retained requester metadata, so ending a scene does not strand a successfully archived Mac file under permanent protection. This lookup stores no local-job-to-guest association, accepts no URL and starts no download. Missing/changed files return pending, failed proof returns failed, and changed files can enter only the existing background hash verification lane.

Verified cache state is bound to device/inode/size/mtime/ctime as well as its checked digest. Missing or modified files, including same-size corruption, are immediately withheld and never acknowledged as archived. Large rehashes/retrievals happen on the background cache lane, not on controls or status/event handlers. Downloads retry at most four times with backoff. TV downloads still independently check bytes and SHA and can try the next source. NAS media/guide responses implement bounded single byte ranges and correct HEAD lengths.

Optional `guide` describes small, timeline-bound original/reference MP3s using `guide_original`/`guide_reference` ticket scopes. Both checked caches must be available before the phone's existing guide endpoint advertises them; no SMB mount is required. This path does not record user audio. The phone still checks hashes and decoded duration before acoustic alignment.

QR creation runs in the Mac worker's separate preparation lane. `/agent/tasks.rooms` exposes the exact current join URL and TV instance only to the authenticated worker. `POST /agent/qr` validates those identities, bounded PNG structure, SHA and byte budget, then publishes atomically. `rooms.qr_ready` becomes false if the file disappears or changes so the worker can republish it.

## Offline verification

`tests/test_ktv_coordinator.py` covers direct API credential scopes, complete/stale readiness, bounded recovery using request substitutes, immutable manifests, first playback before archive, cache corruption/fallback, source/model degradation, media ranges, QR publication/repair, guest preservation, explicit lifecycle/retention, stale starts/closes/unmutes, NAS restart and event delivery. `tests/test_ktv_public.py` retains the legacy bridge and guest contracts. `tests/test_ktv_tv_updates.py` 验证电视权限、下载、单调发布、中断恢复和私有文件校验。All fixtures use isolated storage and synthetic media; no household device, real microphone, playback, model or live NAS is needed.
