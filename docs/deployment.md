# 部署自己的 Friday

## 1. 基础控制面

安装 Python 3.11+、uv，克隆后运行 `./scripts/install-home.sh`。基础环境仅包含
Web 控制面与合成示例，不下载模型、Miloco SDK 或家庭数据。

`./bin/home doctor` 报告当前依赖与连接边界。`./bin/home serve` 在
`http://127.0.0.1:8740` 提供 Console。默认只读示例不能控制真实设备。

## 2. Home Assistant

优先复用已有 HA。通过 Console 设置中的发现与 OAuth 登录连接；多实例时选择目标。
纯 CLI 的只读接入可在本机设置 `HOME_HA_URL`、`HOME_HA_INSTANCE`、`HOME_HA_TOKEN`，
然后执行 `./bin/home --mode ha-read-only serve`。不要把真实 token 写入命令示例或聊天。

需要新 HA 时，在准备运行 HA 的 Linux 主机执行：

```sh
python3 scripts/setup-dependencies.py prepare
# 编辑忽略目录 data/deployment/.env 中的 TZ，选择自己的时区。
python3 scripts/setup-dependencies.py xiaomi-home
python3 scripts/setup-dependencies.py ha-up
```

生成的 Compose 固定 HA 版本，使用 host network 与私有持久卷；默认没有 privileged
权限或 USB/蓝牙设备映射。按实际硬件需求显式添加，不能据此声称无线设备已经可用。
首次打开 `http://<your-linux-host>:8123` 创建自己的管理员。HA Container 不含 Supervisor
或应用商店。官方安装要求 Docker Engine；macOS 应使用 Linux VM/独立 HA 主机，
不要把 Docker Desktop 的网页可访问误认为组播/蓝牙完整兼容。

`xiaomi-home` 从固定上游版本复制集成到新建的私有配置目录；已有集成不覆盖。若 HA
已经运行，重新启动 HA，在 设置 → 设备与服务 → 添加集成 → Xiaomi Home 完成官方
登录，选择你的区域、家庭和设备。账号选择和设备授权只能在上游页面完成。

停止新建的实例：`python3 scripts/setup-dependencies.py ha-down`。保留持久卷；不删除
用户数据。更新之前自行备份，并显式修改版本后复核。

依据：[HA Linux 安装](https://www.home-assistant.io/installation/linux/)、
[HA Container 维护](https://www.home-assistant.io/common-tasks/container/)、
[Xiaomi Home 安装与隐私说明](https://github.com/XiaoMi/ha_xiaomi_home)。

## 3. Miloco（可选）

Friday 基础运行不依赖 Miloco。摄像头能力需要账号、设备支持、SDK/媒体协议以及
独立安装的上游服务；不是安装 Friday 后自动开启摄像头。

```sh
python3 scripts/setup-dependencies.py miloco-source
```

此命令仅克隆固定版本到 `data/upstream/miloco`，不执行安装器。让 Agent 读取其中的
`scripts/install-guide.md`，再按自己的平台读取 OpenClaw 或 Hermes 的完整指南。
Miloco 当前安装入口只支持这两个平台；Codex 可以协助部署这些平台，但不能把自身
声称为已支持的 Miloco plugin host。依官方固定版本 Release 安装；在其本机 UI 登录。
不要把最新版脚本直接当作已审计固定版本执行。

上游版本：[Miloco v2026.10.16](https://github.com/XiaoMi/xiaomi-miloco/tree/v2026.10.16)、
[固定版本安装入口](https://github.com/XiaoMi/xiaomi-miloco/blob/v2026.10.16/scripts/install-guide.md)。

控制面可选的媒体解码依赖：`uv sync --locked --no-dev --extra camera-media`。
原生 SDK 不打包到本仓，按上游许可证及平台限制安装。不要将旧版本 SDK 的一次成功
扩展为新版本兼容性保证。`homeplane/embodied/runtime-manifest.json` 仅是可选组件的
版本/摘要参考，不是已经安装或授权的证明。

## 4. 设备、房型、Agent

Friday 从当前 HA 注册表读取稳定 ID、区域、实体和服务能力，不要求特定品牌型号。
多个同名对象需要明确选择。MIoT 原生扩展按已导入 spec 的属性 URN 解析 SIID/PIID；
缺少/歧义/未知规格不发送猜测指令。HA 控制路径不依赖该原生扩展。

[房型导入](layout.md) 使用你自己的米家导出，默认不包含房型、方向或设备坐标。
可选 ARL 只连接你自己的实例；`scripts/install-arl-sdk.sh /path/to/ARL` 安装本机 SDK。
ARL 不属于基础依赖，缺少可信身份/任务契约时模型功能明确不可用，确定性控制可独立运行。

摄像头、人物识别和动作实验主要面向 macOS，并需要独立的本地模型、原生构建和 SDK。
`uv sync --locked --extra vision --extra camera-media` 只安装声明的软件依赖，不下载
模型或获取授权。`python3 scripts/embodied/runtime.py --check` 列出缺失输入，
`--build-native` 编译当前主机的 Vision worker 与摄像头 socket helper，不安装或启动它们。

## 5. 验收顺序

依次记录：基础服务健康 → HA 登录 → 当前注册表读取 → 自己的户型导入 →
单设备动作预览/授权 → 实际用户确认 → 可选摄像头/音频/模型。发现、注册、HTTP200、
ACK、playing 或帧数都不能证明物理效果、音画同步或识别准确率。
