# 部署自己的 Friday

## 1. 基础控制面

安装 Python 3.11+、uv，克隆后运行 `./scripts/install-home.sh`。基础环境仅包含
Web 控制面与合成示例，不下载模型、Miloco SDK 或家庭数据。

`./bin/home doctor` 报告当前依赖与连接边界。`./bin/home serve` 在
`http://127.0.0.1:8740` 提供 Console。默认只读示例不能控制真实设备。

## 2. Home Assistant

优先复用已有 HA。首次启动桌面版会直接打开“连接你的家”；Web 示例首页和“连接”页
也显示同一清单。点击“查找并连接”：只有一个实例且只有一个地址时会进入它的登录页；
多个实例或地址需要选择。没有发现结果时可直接填写完整地址，不必编辑配置文件。
完成 Home Assistant 官方登录后，点击“连接并读取设备”。地址、授权、实际家庭读取
分别显示完成状态；服务离线时保留已有授权，可重试或更改地址。
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

`xiaomi-home` 安装 Xiaomi Home v0.5.0，校验官方提交
`75b3acb6325a3a8d295688b13904737d6738a6a5`，使用独立版本缓存；已有集成不覆盖。若 HA
已经运行，重新启动 HA，在 设置 → 设备与服务 → 添加集成 → Xiaomi Home 完成官方
登录，选择你的区域、家庭和设备。账号选择和设备授权只能在上游页面完成。

停止新建的实例：`python3 scripts/setup-dependencies.py ha-down`。保留持久卷；不删除
用户数据。日常启动不会自动更新依赖。

### 已有 Xiaomi Home 的离线升级

v0.4.3 的云设备刷新在网络异常后可能不再重试。官方 [#1555](https://github.com/XiaoMi/ha_xiaomi_home/pull/1555)
在 v0.4.6 加入异常后的重试；本脚本固定的 [v0.5.0](https://github.com/XiaoMi/ha_xiaomi_home/releases/tag/v0.5.0)
包含该修复。不要把网络恢复或 HA 网页可打开当作设备状态已恢复。

先暂停互动，在 HA 仍运行时记录当前实例的精确容器标识，并准备来源：

```sh
python3 scripts/setup-dependencies.py xiaomi-home-source
```

正常停止 Friday 与同一台 HA，备份该 HA 完整 `/config`（包括 `.storage`）和 Friday
私有配置。确认容器已停止，再执行下面的升级命令；将示例变量设为已核对的容器标识：

```sh
python3 scripts/setup-dependencies.py xiaomi-home-upgrade --ha-container "$friday_ha_container"
```

Linux 的 Docker 需要 sudo 时，先 `sudo -v`，再追加 `--docker-sudo`。
非默认源码或 Docker 路径可用 `--project`、`--directory`、`--docker-executable`。
升级只适用于私有部署目录下的本地 bind mount；其他 HA 安装方式按原维护入口处理。
脚本会核对容器已停且 `/config` 指向当前目录，完整暂存新集成，并将旧组件保留在
`data/deployment/integration-backups/`。替换文件失败时恢复原组件；启动后的版本迁移
若需回滚，应在再次停机后恢复完整 HA 备份，不能仅换回 Python 文件。

重新启动 HA 与 Friday 后，核对集成版本和加载状态、原实体标识、规则、控制范围、
绑定设备正常且未过期的状态、摄像头及原暂停设置。通过后才恢复互动并由使用者验证
实物动作。依赖安装、服务启动、设备状态和物理效果分别记录。

依据：[HA Linux 安装](https://www.home-assistant.io/installation/linux/)、
[HA Container 维护](https://www.home-assistant.io/common-tasks/container/)、
[Xiaomi Home 安装与隐私说明](https://github.com/XiaoMi/ha_xiaomi_home)。

## 3. Miloco（可选）

Friday 基础运行不依赖 Miloco。摄像头能力需要账号、设备支持、SDK/媒体协议以及
独立安装的上游服务；不是安装 Friday 后自动开启摄像头。

“连接”页的“摄像头与互动”单独标为可选，不阻止家庭设备连接。安装与授权入口直接
可见。已有旧版兼容服务的用户可在“设置摄像头连接 → 已有兼容的 Miloco 服务？”中
填写地址并检查服务与米家绑定；这项检查不取流或开启识别。当前 Miloco 插件按下方
官方平台指南安装，不能把旧版 HTTP 服务配置当作当前插件已经接入的证明。

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
桌面应用会向既有 ARL 注册名称、图标与场景，并维持会话；注册本身不运行模型。
应用平台版本要求、场景套餐及界面连接字段见 [ARL 应用会话](arl-application.md)。

摄像头、人物识别和动作实验主要面向 macOS，并需要独立的本地模型、原生构建和 SDK。
`uv sync --locked --extra vision --extra camera-media` 只安装声明的软件依赖，不下载
模型或获取授权。`python3 scripts/embodied/runtime.py --check` 列出缺失输入，
`--build-native` 编译当前主机的 Vision worker 与摄像头 socket helper，不安装或启动它们。

可选声音复核使用 macOS SoundAnalysis，不下载第三方声音模型。源码部署可通过
`python3 scripts/embodied/runtime.py --build-sound` 显式编译到本机可选运行环境；编译
不会开启摄像头、声音输入或运行分类器。随后在“家中提醒 → 监测设置”开启声音复核。
首次处理已授权镜头的声音时，才核对系统分类器实际提供的类别。构建中间文件使用
用户指定的 CodexBuilds.noindex 缓存；声音原始数据只在有界内存中处理。

可选的 [本机画面复核](local-video-review.md) 使用独立、按需加载的 MLX 子进程。
默认关闭，不随基础部署下载模型或开启监测。

## 5. 验收顺序

依次记录：基础服务健康 → HA 登录 → 当前注册表读取 → 自己的户型导入 →
单设备动作预览/授权 → 实际用户确认 → 可选摄像头/音频/模型。发现、注册、HTTP200、
ACK、playing 或帧数都不能证明物理效果、音画同步或识别准确率。

## 6. 设备控制权限

HA 登录与读取成功不会自动授予 Friday 设备写入权限。首次连接可以先查看状态；
控制范围由本机操作员单独设置，不能通过登录回调或发现结果扩大权限。

本机操作员应根据当前 HA 注册表与设备能力，审阅 `HomeDeviceAuthority`
（`homeplane/action_contracts.py`）的目标实例、操作范围、可选实体限制与有效期，
再将授权保存在忽略的私有目录。Web 服务使用其配置目录下的 `action-authority.json`；
桌面版使用其私有 embodied 目录下的 `private/action-authority.json`，配置后重新连接。
不要复制其他家庭的实例或实体标识。授权文件不是 HA 令牌，不需要在聊天或公开仓库中提交。
已授权的控制可以在“连接 → 高级连接设置 → 家庭数据”中暂停或恢复；暂停不会撤回已发送的指令。

## 7. 家庭内网应用

[宝宝起名工作台](baby-naming.md)作为独立的 NAS 服务部署，通过 Friday 家庭内网门户使用。
