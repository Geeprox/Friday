# Architecture

`homeplane/` is the current control plane; `friday/` retains the optional legacy
camera-oriented API. The default homeplane adapter is the explicit Simulator.
HA ingress reads states and area/device/entity registries. Action planning and
execution have separate capability, authority, freshness and idempotency checks.
Unknown results do not trigger blind retries.

The browser renders source-backed state and optional imported geometry.
`home_layout.py` validates and privately persists Mi Home imports.
`home_presentation.py` maps discovered functions to generic display styles.
`embodied/` owns optional camera workers, speech, gestures and bounded recognition;
it is not required to run the Console. Device discovery does not enable gestures.

The macOS shell is a client of a user-owned household service. Its private paths
are derived from the current user; it does not embed an author's repository path,
login session or signing identity. NAS and People content stay local. Optional
ARL provides the model-runtime boundary and is not a prerequisite for simulation.

## 手势响应记录

`InteractionEngine` 生成事件时记录 `triggered_at`（Unix 秒）、`binding`、`revision`、
`gesture` 和 `scope`，与该次事件的 `target`、`action` 一起保留触发时的规则信息。
单调时钟 `observed_mono` 继续仅用于执行时效检查；展示时间不参与设备授权或发送判断。

设备命令返回后，worker 将上述字段与回报写入 `InteractionFleet.receipts`，由
`/api/embodied/status` 的 `interactions.events` 提供。原有 `at` 仍表示回报记录时间，
不替代触发时间。记录只保留在进程内，最多 12 条，重启后清空；不新增持久化或媒体采集。
公开状态不复制事件中的人物、追踪、会话或单调时钟信息。

互动页展开“最近的手势响应”后，以表格显示最近 5 条，按触发时间倒序排列，时间使用
浏览器本地时区。规则描述使用事件中的手势、范围和设备动作，不从当前配置反推，
避免编辑或删除规则后改写旧事件的含义。设备和镜头名称由当前清单解析，失效时明确
显示不可用；旧记录缺少触发时间、手势或规则信息时显示“未记录”，不猜测补齐。
每条记录保存命令返回时的回报快照，不随后续设备状态持续更新；设备回报与现场实物
确认继续分别处理。

页面回归使用 `fixtures/homeplane/interaction-history.json` 的固定合成事件。复现截图时，
在仓库根目录运行 `python3 -m http.server 18742 --bind 127.0.0.1`，访问
`http://127.0.0.1:18742/fixtures/homeplane/interaction-history.html`。该示例直接加载产品的
`interactions.js` 和样式，只允许读取固定合成响应，拒绝写入，不连接真实服务。
