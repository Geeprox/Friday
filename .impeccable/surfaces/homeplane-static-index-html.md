---
version: 1
slug: "homeplane-static-index-html"
primary_target: "homeplane/static/index.html"
related_targets: ["homeplane/static/interface.css","homeplane/static/home-experience.js","homeplane/static/home-status.js","homeplane/static/connection-setup.html","native/FridayDesktop/Views/ContentView.swift"]
---

# Friday household supervision

Mode: Operate. Web Console and macOS shell. Confirmed product record: PRODUCT.md.

## Direction contract

THESIS: A background household steward made legible. Opening Friday answers three questions: how the home is, how Friday is running, and what needs the user's attention. Manual device operation is a supporting path, not the home page's purpose.

OWN-WORLD: The approved Friday icon is the visual authority: charcoal matte metal with folded white paper. Neutral paper work areas, graphite controls, restrained semantic colour, a compact system-sans scale, real approved artwork. No arbitrary brand recolouring, decorative glass or repeated card dashboards.

STORY: Read the 3D household state and operate in place; inspect quiet service/resource summaries; follow a specific attention item to its device, task or incident. Internal processes and raw errors stay inside disclosure controls. Existing jobs run autonomously; only decisions and failed recovery ask for intervention.

FIRST VIEWPORT: A compact navigation rail, a 56px source toolbar and a 28px title. An imported 3D home takes the main work area; a narrower right column holds service states, measured CPU/memory/latency and prioritized attention rows. Without imported geometry, an honest import state occupies the space with a direct device-list escape. Devices use room navigation, grouped rows, immediate controls and an adjacent or inline detail pane. Mobile stacks the same work areas with labelled navigation. Safety opens on recent incidents; monitoring configuration is a disclosure.

FORM: User-pinned approved Friday identity and user-confirmed background-steward architecture override the earlier rejected blue/card direction. seed:0bbe4960b462f190 remains the session reference, not aesthetic authority. Code-first application screen. The existing Friday icon is copied unchanged with origin metadata. Games is a consumer storefront with individually authored illustrative covers, browse/filter/detail/play flow, honest player counts and prototype labels. Interaction and Games are primary navigation destinations.

SIGNATURE INTERACTION: A line-diagram motion library explains every supported hand and body input. A bounded teaching session progresses from waiting for a person, through recognition, to a single legible success and an opt-in local-device chime. Practice consumes already-running sources and suppresses gesture device dispatch until explicitly closed or expired. Nonessential diagram loops play only while visible and honour reduced motion. Voice shows all cameras, never a mandatory single room selector.

APPROVED HAND REFERENCE (2026-11-04): User approved the Godot XR Tools CC0 hand preview in this chat. Keep its source mesh/poses, orthographic white fill and smooth charcoal outline, with actual open-palm → target → hold → reset. Replace the rejected authored hand drawing with this source. Explore practical official common poses and wire supported recognizers to bindings and isolated teaching. Asset license does not establish recognition accuracy.

Werewolf phone join recovery: persist only the exact pending join intent in current-tab session storage before sending it. A lost response or reload resumes that room and seat with the same random recovery identifier; it does not reserve another seat. The brief recovery state uses the existing neutral join layout and two clear actions, “重试入座” and “更换房间”, with no modal or infrastructure copy. An explicit different invitation supersedes the pending intent. Acknowledgment must match the requested room and seat before storing the personal credential and clearing recovery state. Removed players and closed/replaced rooms remain revoked. Recovery neither exposes roles automatically nor provides public/name-based credential lookup. Actual phone rendering and LAN acceptance remain open.

狼人杀在入座和首夜之间设置认牌环节。手机沿用浅色纸面，按住显示自己的身份，松开收起后才启用「我已看完」。公屏和主持端原地更新确认人数，全员确认后主持端才能点击「开始首夜」。等待期间没有计时，返回入座前提示会重新发牌。画面同步时暂收身份牌，恢复后只显示当次发牌的私有状态。

狼人杀在自由发言和平票发言时提供一个点名选择器。举手标在现有座位和选项内，镜头提示刷新时保留选中的座位和键盘焦点。确认后主持端突出该座位，电视显示「发言中」，被点名的手机显示「轮到你发言」。窄屏将选择器和确认动作排在同一行。电视断线和对局暂停时暂收发言提示，恢复后按当前对局读回。

狼人杀结算页的「再开一局」回到原房间的准备页。保留座位，已坐满时收起扫码区。有空位时重新显示邀请。手机保留自己的座位并提示重新准备，公屏保留同一链接并更新准备人数。上一局的票型和身份揭晓在新局开始时收起。

Werewolf camera alignment: keep the native seat fields inline. “显示画面” reads an already running source beside those fields and overlays the numbers from that same decoded image. This is an explicit local host view, never a capture-start command or a phone/TV feed. Image and choices retire together on expiry, hiding or leaving the lobby; delayed replies cannot revive a retired view. Keep the existing paper/graphite controls, accessible numbered alternative and source-bound binding/readback. No camera, identity or physical accuracy claim follows from synthetic rendering.

Seat correction stays inside the same native chooser: show which seat owns an unavailable number, disable that choice, and offer “取消当前对应” only when that seat has a saved mapping. Releasing it keeps the player and ready state. Waiting for a fresh camera must read “等待画面恢复”; a current picture without the bound person may say “原位置暂不可见”, without declaring that the camera or person moved. Do not replace fields or repeatedly rewrite unchanged native options during picture refresh. A correction changes the existing room version and cannot be undone by an old clear request.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Scope and evidence

摄像头详情以画面为主体。同一个按钮查看和结束预览，「回看录像」在旁边打开当前镜头的录像。实时查看时显示镜头模式和方向键，物理开关单独放在底部。巡视中同一按钮变为「停止巡视」，画面暂时断开时仍可操作。下探、完成和中断复用画面下方的状态行。窄屏先显示画面和回看，再显示模式和方向键。

Synthetic preview only. No live camera/microphone/model/TV/device activation. Desired gaze/wake confidence semantics and configurable wake words are captured as product truth; correcting the full speech execution chain and a Rust migration are separate functional changes, not claimed by this UI refactor. Host resource sampling is passive and bounded, with null values when unavailable. Background service and safety/event summaries read existing owners and never start them.

持续婴儿哭声复核使用现有提醒列表、首页入口和详情。符合三段同源证据时显示“疑似持续婴儿哭声”，保留镜头名、时间、关键帧和查看附近录像入口。普通哭声沿用原名称。设置页以本机已知标签和声音复核开关显示可用状态。照护行为仍显示原能力状态。

「趴卧复核镜头」使用本机画面复核内的一项选择。默认关闭，保留用户选定的镜头和暂停状态。镜头来源变化时，请使用者重新选定。首页和提醒详情共用「疑似趴卧」标题，显示镜头、时间和两段画面各一张关键帧，其余画面按需查看。窄屏使用同一套选择与返回动线。

快速搬动的短窗口在首页和提醒列表共用「快速搬动」标题。详情先呈现完整四帧，用相对时间区分动作前、中和后段，回看、确认与误报操作接在画面之后。

Generated reports and screenshots remain under ignored .artifacts/design-20261103/. The synthetic preview's room geometry and devices never ship as defaults.

Task attention: Home names the actual task and links an immutable notice to the automatic-task tab, where a quiet row shows the execution time and result. A missed or unavailable one-off task offers “重新安排”; an uncertain device outcome offers “查看设备”. “已了解” acknowledges only that notice and never retries execution. The existing polling owner updates keyed rows without replacing the focused control; removing a focused notice moves focus to the next notice or the workspace heading. A changed/handled URL stays understandable instead of opening an obsolete editor. Native controls, paper surfaces and dividers remain the visual structure. No new modal, automatic sound or extra polling owner. Current actual browser/native capture remains required; inert DOM evidence is insufficient for visual finish.
