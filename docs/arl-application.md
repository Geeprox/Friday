# Friday 的 ARL 应用会话

Friday 家庭服务使用拥有者 SDK `arl.application_session.ApplicationSession`，连接当前账户的
`~/Library/Application Support/ARL/runtime.sock`。启动注册、10 秒心跳、30 秒会话租期、断线/服务重启重新握手和退出关闭均由 SDK 生命周期承担。Friday 不启动 ARL 副本。

桌面家庭服务在其独立生命周期注册，不依赖 HA 已连接。纯 Simulator/Demo 不自动注册真实 ARL；测试与合成服务应显式注入替身，Unix socket 验证必须指定临时 SDK 宿主。

稳定应用 ID 为 `com.friday.home.desktop`，展示名 Friday、图标 `sf:house.fill`。名称和图标是展示元数据，身份边界是同账户 kernel PID、进程 birth 与 executable；不声称供应商签名背书。

注册同时通过 SDK `icon=` 上传实际 AppIcon：安装包读取 `CFBundleIconFile`，源码运行读取正式 `Friday.icns`。提取原始 64×64 PNG，仅去除 PNG 附加元数据，不缩放、不更换像素；来源 ICNS 摘要及真实成员标签随请求发送。每次重新握手都会补传。`sf:house.fill` 仅作为图标未获接收时的展示回退。

原始 AppIcon 的 64px 成员为 `ic12`，ARL 的图标合同只接受 `CFBundleIconFile:icp6`，首次上传返回 `application_icon_invalid`。现已在正式图标容器增加 `icp6` 表示，复用原 `ic12` PNG 的同一像素流；所有原成员字节保持不变，图标构建脚本也保留这一格式适配。新的实际 ICNS 来源摘要与 7,047 字节 PNG 已获正式宿主接收，最后探针时已安装的 ARL 包为 build 152。`application.icon.registered` 与应用连接分别报告；旧资源或不支持的图标合同仍显示独立的图标错误，重新握手时补传。

| 场景 | task_kind | priority | package_id | deadline_ms | max_output_bytes |
|---|---|---|---|---|---|
| voice_intent / 语音意图解析 | household.voice.intent | interactive | low | 30000 | 8192 |
| home_planning / 家庭操作建议 | household.plan | interactive | high | 30000 | 8192 |

任务只通过 application.tasks.submit.v1/get.v1/cancel.v1 使用 ARL 原持久队列。消费者不传模型、推理强度或服务档位；由 ARL 在准入时解析并固定套餐。Friday 校验准入场景/套餐/优先级、最终模型、原生线程/轮次确认、推理强度/服务档位、输入/输出摘要和模拟标记。模拟回执不能进入真实操作路径。

复杂语音的工作进程通过既有受限 worker capability 向 Friday 宿主请求 voice_intent；宿主统一持有应用会话。该接口不接受场景、模型、供应商或设备操作参数。请求 ID 在发往 ARL 前持久保存；本机只保存去重摘要和准入回执，不保存提示或结果，不另建推理队列。已知 job 重试只 get；未知提交回执重试仍使用原 request_id。

这里的“不保存”仅指 Friday 自己的回执库。复杂任务的文字提示（可能含转写和选定家庭上下文）及结果进入 ARL 统一持久队列，由 ARL 的保留策略管理；不宣称它们只存在内存或在退出 Friday 时自动删除。音频本身不会由此提交。

旧 `friday/` 相机应用的通用家庭规划继续要求独立的 `arl_model_tasks_enabled` 本机选项，默认 false；注册不会启用原先未授权的家庭上下文发送。旧供应商模型字段不再决定该入口的模型，套餐配置以 ARL 为准。当前原生语音的输入暂停和交互许可仍按原配置校验。

ARL 模型注册不授予家庭、NAS、Apple、凭据或设备操作许可。确定性动作、人物身份、语音输入暂停与结果时效仍由既有领域校验。规则分析及跨应用家庭查询原有未组合的权限接口没有因握手开放。

## 给界面消费者的投影契约

Web SDK 原始状态新增 `application`：`app_id`、`connected`、`state`、`error`、`model_service_accepted`、`identity_basis`、`publisher_attestation=false`、`device_authority=false`、`scenarios`、`heartbeat_seconds=10`、`lease_seconds=30`。
其中 `application.icon` 单独投影 `registered`、`source_resource`、`sha256` 和 `error`。

桌面 `/desktop/status.arl` 新增 `applicationSession`：`appID`、`connected`、`state`、`error`、`modelServiceAccepted`、`identityBasis`。现有 `ready` 仍仅表示健康通信。

界面统一调用 `arlRuntimePresentation(rawStatus)` 或 `arlPresentation(desktopStatus)`：

- `runtimeConnected`：最近健康通信证据有效。
- `applicationRegistered` / `connected`：健康通信有效且本进程应用握手 confirmed。健康检查、ready 或进程 running 都不能单独令它为 true。
- `modelServiceAccepted`：握手后 ARL 接受声明的模型服务能力；不证明已运行真实模型。
- `state`：`connected`、`upgrade_required`、`registering`、`handshake_required`，以及既有 disconnected/not_running/sdk_missing/disabled/unsafe_configuration/starting/stale/refreshing/unknown。
- `label`、`description`、`recovery`：由该投影模块统一提供。`taskReadiness=unverified`、`identityVerified=false`、`modelCapabilityVerified=false` 不因握手推广为家庭身份、操作授权或模型验收。

Native 使用 `ARLStatus.applicationSession` 和 `connectionPhase/connectionDetail/connectionRecovery`，语义与 Web 一致。界面消费方不要直接以 ready 或自行检查进程来设置连接状态。

## 验收边界

当前适配需要含上述应用平台能力的 ARL 服务与配套 SDK。本次源码适配在 build 150 及后续正式宿主通过真实 `connection.open → connection.confirm → connection.ping → connection.close` 元数据探针，验证名称、展示图标引用与两个场景，模型任务提交为 0。最后一次正式宿主探针已接收兼容后的真实 AppIcon，同次读取的 ARL 安装包为 build 152。安装包版本与宿主方法回执分别记录。隔离 Unix socket 测试另外验证了合成任务、共享队列、幂等、宿主重启与租期重新握手。

2026-11-08，这些接线已进入本机安装的 Friday 0.6.1/build 57 体验版。回读确认 `arl.py`、`arl_application.py`、`arl_icon.py`、Web 状态模块及 Native 状态模型与安装包绑定清单一致。但所复用的安装运行环境缺少 `arl.application_session`，且 Client.open_app_session 尚无 scenarios 参数，会话启动会得到 application_sdk_upgrade_required；握手仍待补齐完整兼容的 SDK。开发环境和安装环境都标为 SDK 0.1.2，因此必须检查模块/接口，不能只比版本字符串。共享环境同时被旧版恢复包引用，需通过后续可回退的 SDK 更新处理，本轮未改环境或再次安装。

安装任务没有代用户启动 GUI 或服务；本次回读也没有重新运行已安装应用的握手、模型或家庭功能。因此安装文件就位、早期正式宿主元数据探针、实际 App 会话与整机验收仍是不同门槛。五档真实模型调用由 ARL 会话统一验收。`python -m scripts.check_arl` 仅对既有共享宿主做元数据握手与心跳探针，随后关闭会话，不创建 ARL 副本或提交任务。

## 固定 SDK 输入与唯一导入门槛

后续桌面候选使用经过来源核对的 MIT SDK 子集，复制到 `Contents/Resources/product/vendor`，不原地升级 build 57 和 build 56 恢复包共用的 Python 环境。来源固定为 [Geeprox/ARL a48e8bf](https://github.com/Geeprox/ARL/commit/a48e8bfd15b9ff7716a6a52a3ef9fcdbe915a585)，Git tree 为 `33f2c7d556dcacc050f0a3edaacf4a248c27de8f`。这是公开提交的来源绑定，提交未签名，不是发布者签名背书。模块保持上游原字节，保留 MIT LICENSE、NOTICE 和项目元数据；不包含 CLI、宿主安装器、解释器、权重、账户和私有适配器。共享模块中存在服务类型定义，但导入检查不构造或启动服务。

唯一入口为 `homeplane.arl_application.sdk_compatibility()`。在选定 Python 的独立 `-I -B` 进程中，先将包内 `product/vendor`、`product/app` 放到导入路径前端，再调用一次。调用方只消费 `compatible: bool` 和固定 `code`，不得复制接口要求、构造 Client/ApplicationSession/Runtime 或尝试注册。所需模块、接口参数及预期 manifest 摘要只维护在 `homeplane/arl_sdk_requirements.json`。

默认入口依据实际 `arl` 包位置寻找同根的 `ARL-SDK-MANIFEST.json`，校验清单的固定 SHA-256、完整文件集、每文件 SHA-256/Git blob/大小、路径和模块映射后，才从已验证内存字节导入。私有 `arl.private_compat` 明确缺席，沿用上游公共默认策略，且禁止回退到系统副本。不接受已加载 ARL 或多线程进程。校验失败仅返回固定状态，不启动宿主或安装依赖。

返回值还包含 `contract`、`protocol`、`manifest_sha256`、`source_commit`、`sdk_version`、实际导入模块的相对路径和摘要、`denied_events`、`new_threads`、`registration_attempted=false`、`model_attempted=false`。`compatible` 仅表示冻结输入可以导入且接口满足 Friday；不表示身份、宿主协议握手、模型或家庭操作已验收。

固定错误码为 `sdk_not_found`、`sdk_manifest_missing`、`sdk_manifest_pin_mismatch`、`sdk_manifest_invalid`、`sdk_unsafe_path`、`sdk_inventory_mismatch`、`sdk_source_changed`、`sdk_probe_requires_fresh_process`、`sdk_module_not_pinned`、`sdk_interface_mismatch`、`sdk_import_failed`、`sdk_side_effect_attempt`。成功码为 `compatible`。可选 `vendor_root` 与 `expected_manifest_sha256` 供交付输入验证使用；显式摘要必须来自审核记录，不能从待验文件自行取值后当作可信来源。

2026-11-08 的冻结输入与交接回执保存在本机忽略目录 `.artifacts/arl-sdk-input-20261108/`。48 个 Python 模块包含所有静态嵌套导入及父包，共 522141 字节，无第三方运行依赖；上游动态可选私有组合明确排除。封包端负责将完整输入复制、读回并纳入自身来源绑定和签名，然后独立执行此门槛及原 backend 定义导入门槛。现装 build 57 和完整旧环境保持原样，实际新候选签名、安装与注册仍属后续验收。

## 仍待组合的消费链路

- 旧类型化规划 actor 仍要求拥有者的精确契约、源摘要、执行策略与审核授权。现存 planning-candidate-v2 的 `homeplane/arl.py` 摘要与当前产品不符，源校验仍拒绝；不能据通用应用注册开放该旧 actor。消费方经 home_planning 生成 RuleIR 的新任务链路是另一条接线。
- 跨应用家庭查询仍需单独组合查询权限与数据消费者；模型场景注册不提供家庭数据权限。
- 共享本机 Laya 的类型化接口与通用 Codex 场景是不同合同，继续保留原范围；五档模型验收不能当作 Laya 实测。
- 复杂语音与家庭规划已改为通用场景源码，但未对家庭语音或家庭上下文运行真实模型。真实输入质量、领域解析和动作确认仍需分别验收。
- build 57 是用户明确要求的当前进度体验版，完整回归仍有已记录的失败；实际可视、真实语音、设备效果和用户接受度待验收。build 56 恢复包由安装任务保留，早期最小注册补丁仅作为体验期的历史参考。
- 已安装运行环境的应用会话 SDK 模块缺失；在补齐前，不能把“ARL 接线已打包”写成“安装版已经握手成功”。
