# Friday

一个本地优先的家庭控制面，把房间、设备、自动化、摄像头和私有录像组织在一起。macOS 桌面可连接米家中枢，Home Assistant 提供可选的设备和自动化集成。实际控制由本机授权与设备能力决定。

Linux x86_64 CPU 适配的独立环境、ARL 入口、验证范围和平台边界见[Linux CPU 运行时](docs/linux-runtime.md)。

Friday 不包含任何真实家庭的设备清单、户型、方向、成员、录像或登录状态。未导入户型时显示空状态；示例环境全部标记为 simulated。

## 五分钟运行示例

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。在项目根目录执行：

```sh
./scripts/install-home.sh
./bin/home doctor
./bin/home serve
```

打开 **http://127.0.0.1:8740**。默认使用合成 Simulator，无需 HA、米家账号、模型或硬件。实际安装耗时取决于网络和主机。服务仅监听本机，不要直接转发到公网。

## 让 Agent 帮助部署

把下面这段交给你的编程 Agent，并让它在仓库根目录工作：

> 阅读 AGENTS.md、docs/deployment.md 和 docs/privacy.md。先执行 python3 scripts/setup-dependencies.py plan，检查当前系统和已有连接。米家请使用我在本机授权的家庭，按固定版本指南准备 MIoT 运行环境。需要 HA 集成时优先复用已有实例。导入我自己的米家户型，分别核对授权、状态读取和实际设备效果。

可复现的环境入口：

```sh
python3 scripts/setup-dependencies.py plan
python3 scripts/setup-dependencies.py prepare
# 以下命令在准备运行 HA 的 Linux 主机执行：
python3 scripts/setup-dependencies.py xiaomi-home
python3 scripts/setup-dependencies.py ha-up
# 可选：下载固定版本 Miloco 源码，之后按其平台指南安装：
python3 scripts/setup-dependencies.py miloco-source
```

详细步骤、macOS/Linux 区别和需要本人完成的账号步骤见 [部署指南](docs/deployment.md)。

## 连接自己的家庭

1. macOS 桌面打开「设置 → 家庭连接」，在「米家直连」查看已接入功能。已有 HA 连接的家庭先通电并保持原连接，让 Friday 核对现有绑定。米家中枢能读回已接入设备后，可切换到米家直连。新发现的受支持设备可在「设备 → 米家待接入」确认接入。
2. 需要 HA 设备或原有 HA 规则时，可展开同页的「Home Assistant」连接原实例。命令行只读入口和两种连接的范围见 [部署指南](docs/deployment.md)。
3. 在米家取得自己的 BakeData/楼层 JSON，再用 `scripts/embodied/import_home_layout.py` 预览与导入。缺失设备坐标保留为未放置。
4. 摄像头、麦克风、人物识别、自动运行、云端模型与播报按使用者选择单独配置。

[能力与限制](docs/capabilities.md) · [房型导入](docs/layout.md) · [架构](docs/architecture.md) · [隐私](docs/privacy.md)

可在“设置 → 智能服务”选择 ARL · Codex、OpenAI API、API 优先或仅本地功能，分别配置
每日请求、文本 Token、图片与语音上限。额度不足时按设置回退，基础设备控制、规则和
KTV 播放保留。云端语音输入与逐曲背景各自单独开启，见[智能通道与用量](docs/intelligence.md)。

## 日常使用

- **我的家**：按导入户型和真实房间查看设备模型，打开房间或设备完成主要控制。没有安装位置的数据保留未放置，不猜测坐标。
- **设备**：灯光、电路开关、窗帘、空调和摄像头采用对应控件；可配置按键按注册模式区分动作触发与电路通断。
- **米家待接入**：桌面版用已有米家授权检查家庭目录。「设备」可接入电源设备及受支持的只读传感器，灯具可使用中枢读回的亮度和色温。新接入的设备沿用米家名称和房间。「摄像头」单独显示新镜头。已开启的镜头默认参与语音和手势，可在详情单独关闭互动。
- **互动控制**：设置语音、手势或指向设备。开启语音后，各台已接入且正在感知的镜头可以同时收音。首路没听清或转写时已过期，Friday 可用另一台镜头对同一句话的收音核对唤醒词。听清唤醒词且镜头处于「跟随人物」时，可以搜索并转向。多人使用手势时，持续比 OK 可以选择自己或接管，再放下手做已绑定的动作。
- **自动化**：运行米家提供的手动场景，管理 Friday 自有规则。外部名称和配置快照保留来源，重建规则需要先处理原有规则；不声称直接编辑上游自动化。
- **回看**：自动恢复已配置的 NAS 连接，静音播放本机保护预览，支持时间线定位和后续片段预取。默认整帧保护；选择性遮挡需要本机模型与相应许可，见[隐私回看](docs/privacy-playback.md)。
- **人物**：相似的匿名观察形成待确认组，可核对、拆分、合并和撤销。归组不是身份认证，登记姓名仍需本人确认。

设备报告、接入成功与实际物理效果分别展示。摄像头授权、网络连接、镜头遮蔽和暂停也分别表达；后台恢复不会开启已关闭镜头。

## 开发与验证

```sh
uv sync --locked --python 3.12 --group dev
.venv/bin/python -m pytest -q --basetemp=/tmp/friday-tests
node --test tests/audio.test.mjs
.venv/bin/ruff check --select E9,F63,F7,F82 homeplane friday scripts tests
python3 scripts/audit_publication.py
```

Node.js 24.5+ 用于 JS 测试及可选米家极客版 worker。首次使用 worker：`npm --prefix workers/geek ci --ignore-scripts`。Miloco 原生 SDK、ARL 私有候选包与真实硬件测试需要额外本机输入，缺失时会明确跳过对应外部集成测试。默认开发测试不需要家庭凭据。

完整媒体回归还需安装 FFmpeg（包含 `ffprobe`）：macOS 使用 `brew install ffmpeg`，
Debian/Ubuntu 使用 `sudo apt-get install ffmpeg`。CI 会安装它们并执行真实合成媒体检查；
本机缺少工具时，相应集成测试明确跳过。

macOS 桌面源码位于 `native/FridayDesktop/`，用 SwiftPM 构建；[桌面构建说明](docs/FRIDAY-DESKTOP-BUILD.md) 说明本机运行环境与签名边界。基础 Web Console 支持 macOS/Linux，无需桌面打包。

页面与组件的布局、交互和视觉约定见[设计规范](DESIGN.md)，后续界面扩展沿用同一套规则。

开发和 CI 使用显式准备、逐文件校验的固定公开 ARL SDK 输入；准备与清理步骤见桌面构建说明，公开客户端和完整宿主的测试边界见[开发说明](docs/development.md)。这不会安装 ARL 宿主、模型或修改已有 Python 环境。

LG TV 菜单栏、电源和系统音量工具由独立项目维护。[LGTVMenu](https://github.com/Geeprox/LGTVMenu)

代码采用 [MIT](LICENSE)。外部服务、SDK 与模型保持各自许可，见 [第三方声明](THIRD_PARTY_NOTICES.md)。Miloco、HA、ARL 应用和模型权重不随 Friday 分发。桌面候选可附带经来源与摘要校验的 MIT ARL Python SDK；它只提供客户端与共享契约，不包含 ARL 宿主、模型或账户材料。
