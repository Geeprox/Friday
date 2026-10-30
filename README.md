# Friday

一个本地优先的家庭控制面：把 Home Assistant 的房间、设备、自动化、摄像头和私有录像组织到同一界面。Agent 可协助部署依赖、解释状态并生成操作预览；实际控制仍由本地权限与设备能力校验决定。

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

> 阅读 AGENTS.md、docs/deployment.md 和 docs/privacy.md。先执行 python3 scripts/setup-dependencies.py plan，检查当前系统与现有 HA/Miloco，再安装 Friday。复用已部署的服务；缺少 HA 时，在 Linux Docker Engine 上准备项目私有 Compose 配置。Miloco 按固定版本的官方指南、对应 Agent 平台单独安装。凭据由我在本机官方登录页面输入。通过 HA 注册表识别设备和房间，导入我自己的米家户型；未确认的能力保留不可用。最后报告实际启动、登录、读取和硬件验收各自的结果。

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

1. 在 HA 安装官方 [Xiaomi Home 集成](https://github.com/XiaoMi/ha_xiaomi_home)，在其页面登录并选择自己的家庭和设备。
2. 首次打开 Friday 的“连接你的家”，点击“查找并连接”并完成官方登录，再“连接并读取设备”。未发现服务时可直接填写地址；多个实例需要选择。也可通过本机环境变量配置只读连接，见部署指南。
3. Friday 从 HA 的 area/device/entity registries 与服务能力读取设备。设备名称、型号、地址和数量均来自当前实例。名称歧义需要用户选择，不按作者家庭做匹配。
4. 在米家取得自己的 BakeData/楼层 JSON，再用 `scripts/embodied/import_home_layout.py` 预览与导入。缺失设备坐标保持未放置，不生成猜测户型或朝向。
5. 摄像头、麦克风、人物识别、自动运行、云端模型与播报是可选功能，需要单独配置和授权。发现设备不会启用动作。

[能力与限制](docs/capabilities.md) · [房型导入](docs/layout.md) · [架构](docs/architecture.md) · [隐私](docs/privacy.md)

## 日常使用

- **我的家**：按导入户型和真实房间查看设备模型，打开房间或设备完成主要控制。没有安装位置的数据保留未放置，不猜测坐标。
- **设备**：灯光、电路开关、窗帘、空调和摄像头采用对应控件；可配置按键按注册模式区分动作触发与电路通断。
- **互动控制**：选择房间和镜头，设置语音、手势或指向设备的控制；镜头关闭时需要用户主动唤醒。
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

macOS 桌面源码位于 `native/FridayDesktop/`，用 SwiftPM 构建；[桌面构建说明](docs/FRIDAY-DESKTOP-BUILD.md) 说明本机运行环境与签名边界。基础 Web Console 支持 macOS/Linux，无需桌面打包。

代码采用 [MIT](LICENSE)。外部服务、SDK 与模型保持各自许可，见 [第三方声明](THIRD_PARTY_NOTICES.md)。Miloco、HA、ARL 和模型权重不随 Friday 分发。
