# Linux x86_64 CPU 运行时（候选代码）

本次适配用 Python 进程替代 Apple Vision、SoundAnalysis 和系统朗读入口，复用 Friday 的房间、设备、权限、时效、稳定器和撤销逻辑。ARL 使用配套的 Linux 宿主。代码仍须在目标机器验收，不能将本页理解为所有功能已完成 Linux 实测；当前发布安装器不会自动安装这些候选组件。

## 能力与边界

| 能力 | Linux 路径 | 验证边界 |
| --- | --- | --- |
| Web、HA、设备与自动化、文件/NAS 等通用模块 | 复用 Python 服务与已有协议 | 各自账号、挂载、设备能力仍须配置 |
| 小米视频、摄像头音频 | 官方 x86_64 SDK，CPU 解码 | 真实视频及摄像头麦克风 CPU 识别已在本机 Docker 验证，使用者确认识别正确 |
| 人体、手势 | OpenCV Zoo ONNX + ONNX Runtime CPU → 既有 Python 规则 | 无 AVX 环境已执行真实推理；距离、遮挡与误识别仍需现场确认 |
| 实验性虹膜注视 | 新 ONNX 后端暂不支持 | 返回 `attention_supported=false`，不伪造注视结果 |
| 猫狗目标 | 既有 YOLO ONNX CPU | 仅 cat/dog，不推断具体宠物身份 |
| 语音识别与回复 | SenseVoice ONNX + VAD；espeak-ng + FFmpeg | 中文音色与 Apple 不同；支持文本播报的设备可走设备合成例外，见下文 |
| 声音复核 | 可选 YAMNet CPU | 只消费已授权两秒摄像头 PCM；不录音、不自动播报 |
| 连续画面复核 | 可选 Qwen3.5-2B / Transformers CPU | 大模型性能未测；超时跳过，不能当成可靠安全监控 |
| AI 任务、会话、取消、回执 | ARL Linux + 固定 Codex CLI | 仍需独立安装、登录及启用模型服务；并非全离线大模型 |
| Apple 提醒事项、日历、信息、备忘录、钥匙串 | 不支持 | 不仿造系统服务成功结果 |
| 方向云台转动、人体/宠物自动跟踪 | 固定 Linux x86_64 SDK ABI + 既有 Python 跟踪器 | 真实左右转动与人体跟随已验证，使用者确认停下后稳定；宠物跟踪未实测 |
| KTV 本地人声分离 | MDX ONNX Runtime CPU，按块分离伴奏和人声 | 本机 Docker 已完成真实 CPU 分离，使用者确认伴奏可用；原生目标 CPU 耗时另测 |
| macOS 原生桌面壳、ARL Control | Linux 使用浏览器入口 | Friday Web；ARL 本机服务设置页及 CLI |

手部模型输出没有逐关节可见度分数。兼容字段使用模型存在门槛，并显式记录 `confidence_source`；左右手分类分数单独保存，不能当成关节准确率。新 ONNX 路径暂不提供原先实验性虹膜几何判断。声音分数跨模型窗口取最低值，短脉冲不扩展成持续事件。

## 准备独立环境

以下命令在 **包含本次适配代码** 的 Friday 根目录运行。使用普通账号，保留原安装和暂停标记；不复制 macOS 虚拟环境、钥匙串、动态库或旧网卡名。系统依赖例子适用于 Debian/Ubuntu，其他发行版安装对应包。

```sh
sudo apt-get install -y ffmpeg espeak-ng libgl1 libegl1 libgles2 libglib2.0-0 libxcb1 libportaudio2
export FRIDAY_EMBODIED_DATA="$PWD/data/embodied"
UV_PROJECT_ENVIRONMENT="$FRIDAY_EMBODIED_DATA/runtime" uv sync --locked --python 3.12 --extra vision --extra local-runtime --extra airplay --extra nas-connection --extra privacy-playback --extra ktv
UV_PROJECT_ENVIRONMENT="$FRIDAY_EMBODIED_DATA/vision-runtime" uv sync --locked --python 3.12 --project native/PortableVision
python3 scripts/embodied/prepare_portable_models.py --download
python3 scripts/embodied/runtime.py --build-portable
```

模型准备脚本需要 Python 3.11+，可以改用上述 `runtime/bin/python`。它只下载固定来源、大小与 SHA-256 的模型，不运行模型；不带 `--download` 时只检查。四个基础视觉模型约 25.6 MB。YOLO、SenseVoice、VAD、摄像头私有配置与授权仍沿已有安装流程准备。缺失时检查会报告，不能靠空文件补齐。

独立 `vision-runtime` 锁定 NumPy、OpenCV、Pillow 和 ONNX Runtime，与摄像头 SDK 的依赖隔离。清单与锁文件在 `native/PortableVision/`；实际模型保存在私有 `data/`，不进 Git。模型和第三方库遵循各自许可证，本仓库的摘要清单不改变 SDK 许可范围。

人体和手部使用 OpenCV Zoo 固定提交 `48b10fa505aeb51fbd127fc60a5c660040e823b7` 的四个 ONNX 模型，逐文件核对 Git LFS 摘要。预处理与坐标还原源码及 Apache 2.0 许可证位于 `homeplane/embodied/vendor/opencv_zoo/`；适配器显式选择 `CPUExecutionProvider`，两条计算线程、一条调度线程，不导入 MediaPipe 原生运行库。模型准备后保存在 `models/opencv-zoo/`。

这条视觉路径不要求 AVX，已在 AVX=false 的 amd64 模拟环境完成模型加载和真实图片推理。检测框 NMS 使用 xywh；人体关节保留可见度与存在分数，手部仍区分左右手分类分数和模型存在门槛。单帧缓存保留原 sample_id，不把旧手势当新观测。

可选环境声音分类仍使用要求 AVX 的 MediaPipe YAMNet；它不属于基础视觉依赖，也不影响 SenseVoice 语音识别。需要时另外执行 `uv sync --locked --python 3.12 --extra sound --project native/PortableVision`（沿用上面的 `UV_PROJECT_ENVIRONMENT`），再运行 `prepare_portable_models.py --sound --download` 和 `runtime.py --build-sound`。无 AVX 时声音分类明确拒绝加载。Docker 保留该可选库和 EGL/GLES 链接依赖，但不会借此启用 GPU。

### 官方 SDK 的固定输入

当前旧版摄像头适配对应 [Miloco v2026.9.11 发行包](https://github.com/XiaoMi/xiaomi-miloco/releases/tag/v2026.9.11)，不能用另一个版本的库冒充通过校验：

- 包：`miloco-linux-x86_64-2026.9.11.tar.gz`
- 整包 SHA-256：`f73065a839f1343a9ff698b3fa2de8b228a992e71aab8227dc9a84e28e97d6ee`
- MIoT wheel：`miloco_miot-2026.9.11-py3-none-manylinux_2_28_x86_64.whl`
- wheel SHA-256：`146fc1b51f6434a78e467fff46e9e4e8c5548d38f37378124f5013f41188a6eb`

由操作者按官方授权安装匹配 wheel 到 `data/embodied/runtime`。旧版有界媒体适配另检查 `data/miloco-venv`；Python 3.12、Miloco 2026.9.11 分发元数据及每个 SDK 文件必须匹配 `homeplane/native_media_sdk_linux.json`，实际媒体调用仍受原所有权与生命周期约束。本次只验证固定包内容和合成 SDK，未启动官方后台。较新的插件式 Miloco 安装见[部署说明](deployment.md)，不是上述旧适配的自动兼容替代品。

Linux 视频默认 CPU 解码。旧配置的 `camera_interface=en0` 等字段须移除，改由 Linux 系统路由选择接口；指定不支持的覆盖会明确拒绝。云台库不匹配时不会中断视频，但方向控制会明确拒绝。Linux 的函数地址由固定 ELF 调用链单独核对，并检查完整 SHA-256、ELF 架构及加载后的代码字节，见 [Linux 云台 ABI 证据](linux-motor-abi.md)。

### 可选的 CPU 画面复核

```sh
UV_PROJECT_ENVIRONMENT="$FRIDAY_EMBODIED_DATA/review-runtime" uv sync --locked --python 3.12 --project native/PortableReview
"$FRIDAY_EMBODIED_DATA/runtime/bin/python" scripts/embodied/prepare_portable_models.py --review --download
```

固定 [Qwen3.5-2B](https://huggingface.co/Qwen/Qwen3.5-2B/tree/15852e8c16360a2fea060d615a32b45270f8a8fc) 原始权重约 4.55 GB。CPU 使用 float32，工程准入要求主机至少 24 GiB 内存、冷启动可用内存至少 16 GiB，进程地址空间限额 16 GiB；这些是保守预算，**不是已测性能保证**。依赖是 PyTorch CPU wheel，不安装 CUDA。无需此功能时不要安装这一环境，也不要启用对应复核开关。

只在用户启用后启动工作进程；加载前逐文件核验，离线加载且拒绝远程代码。启动最多 120 秒，启动后重新向原所有者取当前四帧；推理仍最多 12 秒，迟到、撤销、镜头变化、内存不足均丢弃。不能为适应慢 CPU 放宽证据时效，也没有云端图像兜底。

## 云台与自动跟踪

启用既有摄像头控制授权后，方向控制复用当前本地会话；Python 跟踪器继续消费人体/宠物检测框。保留云台租约、隐私遮挡、目标稳定、镜头代次、跟踪丢失后的搜索与原设置恢复。单次方向命令必须在 1.5 秒有效窗内，间隔至少 0.7 秒；迟到命令不会排队补发。

接口只开放左、右、上、下短步进，不允许任意 SDK 命令。固定 SDK 版本之外拒绝调用；ACK 仅是通信回执，`physical_movement_verified` 始终为 false，不能表示云台已实物转动。

## CPU KTV 人声分离

在 Friday 根目录准备独立环境和模型：

```sh
UV_PROJECT_ENVIRONMENT="$PWD/.artifacts/ktv-audio/separation/cpu-runtime" uv sync --locked --python 3.12 --project native/PortableSeparation
python3 scripts/prepare_ktv_cpu.py --cache-dir "$PWD/.artifacts/ktv-audio/separation" --download
```

模型准备只下载 [UVR-MDX-NET-Inst_HQ_3](https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/UVR-MDX-NET-Inst_HQ_3.onnx)，共 66,759,214 字节，SHA-256 为 `317554b07fe1ea5279a77f2b1520a41ea4b93432560c4ffd08792c30fddf9adc`。不带 `--download` 只核验。缓存目录必须与 KTV 使用的目录一致，自定义缓存时同步替换命令参数。

Linux 自动选择 ONNX Runtime CPUExecutionProvider；FFmpeg 解码、NumPy STFT 与重叠相加都在 CPU 执行。不使用 Apple MLX、Metal 或 CUDA。分离保存为 44.1 kHz 双声道伴奏和人声音轨，沿用缓存身份、取消、超时、输出核验及播放授权。

每段输入最长 30 分钟，工作进程地址空间限制 4 GiB、单文件限制 2 GiB，默认准备超时 30 分钟；按块处理并归一化两条音轨以避免溢出。超过预算、取消或摘要变化不会发布就绪结果。普通测试仅用合成信号及假模型输出验证 DSP、长度对齐和生命周期；真正的人声残留、音乐失真与计算速度需要目标 CPU 上测量，不能承诺实时分离。

## ARL 配套宿主

使用配套 ARL 仓库的 `arl.linux_host`，详见该仓库 `docs/linux-host.md`。Friday 与 ARL 默认共享 `${XDG_STATE_HOME:-$HOME/.local/state}/arl`；`XDG_STATE_HOME` 必须是绝对路径。Linux 不依赖 `~/Library/Application Support/ARL`。

ARL 没有模型配置时不执行 AI 任务；通用服务由独立配置和应用授权决定。`python -m arl.linux_settings serve` 打开本机浏览器设置入口，可管理高德、飞猪、网易云、挂载 NAS、模型 CLI、任务档位和并行数。保存不自动连接供应商；Apple 服务显示不支持。网易云写入需另行勾选，NAS 仅开放已挂载目录的列表和有界读取。凭据使用当前账号私有文件，不冒充加密钥匙串。

## 检查与验收

```sh
"$FRIDAY_EMBODIED_DATA/runtime/bin/python" scripts/embodied/prepare_portable_models.py
python3 scripts/embodied/runtime.py --check --profile full
```

`ready` 只表示列出的前置文件与依赖存在，`readiness_scope=prerequisites_only`；不代表 SDK 登录、模型可推理、相机可见或设备已执行。`hardware_verified` 保持 false。模型实际加载另核验摘要。

本次在隔离的 Linux amd64 容器验证依赖安装、导入和合成协议；主机为 macOS，通过 amd64 容器执行，不能用测试耗时推断目标 Intel/AMD 机器帧率。没有启动真实模型、账号、麦克风、摄像头、音箱或家电。正式验收按单路视频→关键点→手势→语音→经授权设备回读逐步完成，最后由使用者确认实物效果；云台实物动作、CPU 分离效果与供应商账号连通性仍需在目标环境确认。

### 本次回归记录

本轮 Friday Linux 相关回归 351 项通过、15 项条件跳过；随后新增的 CPU Provider 装配与运行时检查 13 项通过（与前一组部分重合，不相加）。覆盖合成 DSP、分离缓存与取消、Linux 云台 ABI、方向步进、过期拒绝、自动跟踪、采集恢复、声音和语音边界。跳过项依赖未装配的固定 ARL SDK 或平台条件，不计为实机通过。ARL 相关回归 59 项通过、1 项 macOS Swift 条件跳过。独立分离环境按锁文件安装，并静态核对实际 ONNX 输入/输出为 float32 `[batch,4,3072,256]`；没有创建真实模型推理会话。

以上合成回归阶段未部署；后续本机容器接入见下文实测记录。代码和镜像仍未提交、推送或发布，原 macOS 家庭配置保留。


## Docker 启动入口

配套项目索引提供 `scripts/build_linux_container.py` 与 `docker/compose.yaml`，从 Friday/ARL 公共源码快照构建 `linux/amd64` CPU 镜像。默认启动隔离模拟 Web、ARL 宿主和本机设置页；不是带家庭账号、摄像头 SDK、权重和真实设备规则的完整镜像。使用独立数据卷，按组件准备模型与账号，不能直接迁移 macOS 虚拟环境。完整命令与停止、恢复方法见项目索引的 `docs/linux-container.md`。

家庭候选镜像可由构建器的 `--camera-sdk` 参数加入固定 SDK，用 `docker/compose.household.yaml` 选择真实 HA 模式。私有状态和授权单独导入数据卷；真实来源失败时启动失败，不回退到模拟数据。CPU 分离子进程的入口不再提前导入主进程的 Web 日志依赖，修复独立环境缺少 `httpx` 时在模型启动前退出的问题。

### 实际模型与家庭连接验证（2026-11-13）

在 Mac Docker Desktop 的 amd64 容器中，已接入既有 HA，认证、注册表及持续订阅正常。通过浏览器麦克风将真人测试语音送入容器中的 SenseVoice CPU 模型，约 2.55 秒返回，使用者确认识别正确；输入只经内存，未落盘、未触发设备动作。中文 espeak-ng 合成已生成有效音频，当时摄像头麦克风链路和家庭音箱播报尚未验收；后续结果见下文。

固定 MDX 模型对 20 秒公开许可音乐完成真实 CPU 分离，首次处理约 73.7 秒，输出两条 44.1 kHz 双声道音轨，使用者试听确认伴奏中的人声明显减少、效果可用。样本为 Karissa Hobbs 的 [Let's Go Fishin'](https://freemusicarchive.org/music/Karissa_Hobbs/Age_of_Flowers/09_Lets_Go_Fishin/)，按 CC BY-NC-SA 4.0 使用 35–55 秒片段；分离衍生音轨保留同许可。该时间来自 amd64 模拟运行，不是原生 Intel/AMD 性能指标。

摄像头握手超时已缩小到局域网发现回复的虚拟网络返回路径：相同发现请求在宿主可收到摄像头回复，在容器中收不到，而 MIoT 心跳正常。仅把广播改为定向发送仍失败。加入回环监听、随机凭据和单台登记摄像头限制的 UDP 转发后，固定 Linux SDK 在 Docker 中完成连接、收包与 CPU 解码，首次有界探测取得 5 帧，未保存画面、未请求云台动作。适配同时覆盖 glibc fortify 收包入口；只改普通 recvfrom 不足以解封装 SDK 收包。转发器不使用 Apple 框架或 macOS SDK，源码可运行于 Linux。

ONNX、声音、KTV 和 Linux 运行时回归 49 项通过；转发器、fortify 收包、预览退出与视觉管道回归另 60 项通过，均使用隔离输入。公开人体图片识别到 14 个有效关节，单帧约 0.39 秒；这不是家庭现场准确率或原生 Intel/AMD 性能指标。家庭动作联动在本次现场测试中保持关闭。


持续运行采样：摄像头 CPU 解码约 20 帧/秒，视觉管道两分钟内 30 次采样全部为 live，最大帧龄 505 毫秒，观测到 7 个不同时间的云台成功回执。公开手部示例也执行了实际掌心与关节两阶段推理，检出一只手及 21 个关节点，约 53 毫秒；这只验证该推理路径，不能替代家庭手势准确率验收。专用 UDP 转发的部署与停止顺序见项目索引 `docs/linux-container.md`。

随后用内存图像核对真实云台：右向短步进后的背景水平位移约 -92.6 像素，左向约 +93.4 像素，各有超过 170 个几何内点且分布覆盖至少 8 个背景网格。验证完恢复自动模式；未保存图像。该结果把实际镜头运动与 SDK 回执分开核对，使用者随后确认镜头能左右跟随，停下后稳定，本次真实摄像头人体自动跟踪验收通过；不扩大为宠物跟踪或其他场景的验收。


### 摄像头麦克风与家庭屏文本播报（2026-11-14）

真实摄像头 SDK 的音频经既有 UDP 适配进入 Docker，由 SenseVoice CPU 识别。本次有界测试收到约 7 秒有效音频，识别耗时约 0.9 秒，使用者确认识别正确；未保存音频或转写文本，未执行识别出的家电指令，测试后恢复原识别与跟踪策略。固定测试短句的自动匹配未命中，因此单独记录使用者确认，不声称逐字匹配通过。此结果验证摄像头输入和 CPU 识别，不等同于持续监听或语音控制全部场景验收。

家庭屏播报沿用 `Speaker` 的通知路径：Docker → HA 的 `notify.send_message` → 已选设备的“播放文本”。`speaker_notify` 必须来自 HA 注册表中使用者选定设备的对应实体，并与音量控制实体核对为同一设备；不得替换为“执行文本指令”。通知参数按集成要求编码为包含一条文本的 JSON 数组。目标和独立 HA 会话只保存在私有持久卷，不写进镜像。

**已接受的纯 CPU 方案例外：** Xiaomi 智能家庭屏 Mini 当前接入的 MIoT 接口提供文本播报，未提供本地音频文件播放入口。这条路径由设备侧完成语音合成，不经过 Docker 内 espeak-ng；是否依赖设备厂商云端未验证，不能宣称全离线。Docker 负责调用，不需要 GPU 或 Apple 服务。支持本地音频播放的其他音箱仍可使用 Docker CPU 合成路径。

本次以 20% 音量执行短句播报，HA 返回接受请求，使用者另行确认听到且内容清楚，实物播报验收通过。结束后已读回确认原音量、静音状态及语音暂停标记恢复，家庭联动保持暂停；保存目标不等于开启持续播报。原 macOS 部署配置未修改。此次沿用已有产品路径，仅接入私有配置并补充实测记录，未改变产品界面。


### Docker 授权绑定与只读播放器状态（2026-11-14）

迁移同一个 HA 实例时，家庭设置、OAuth 凭据和 `action-authority.json` 的 HA 地址必须一致。宿主回环地址改为容器可达地址后，仅导入旧授权文件会使全部控制目标被拒绝；`enabled=true` 不能证明授权与当前来源匹配。核对 HA 实例和注册表、保留原授权备份后，按本次迁移授权更新私有文件的地址，保持实例、操作列表、设备范围和有效期不变。不可删除运行时的地址匹配校验或自动信任任意新地址。修复前暂停自动规则与手势执行，先检查候选设备和页面，不以真实家电动作为列表验收。

设备列表现在将 `media_player.state` 作为只读读数显示，未授权播放器写操作也能呈现待机、播放或暂停状态；未知或离线状态仍保持不可用。此修改不新增音箱控制权限，也不把文本播报成功当成当前播放状态。相关概览、设备控制与手势隔离回归共 85 项通过。

### 镜像集成与发布审查（2026-11-15）

镜像构建实现统一迁至本仓库 `docker/`，项目索引保留 Compose 与兼容入口。`scripts/build_linux_container.py --release` 要求干净来源，核对 `docker/runtime-lock.json` 中 ARL 的固定提交与实际运行文件摘要，再导出经过公开输入审计的上下文。镜像内 `/opt/container/build-inputs.json` 记录实际来源与文件摘要，本地工作区候选明确带有 dirty 标记。

`.github/workflows/container.yml` 作为源码 CI 的后续工作流，只在同一提交的回归与秘密扫描通过后构建。隔离容器使用随机本机端口与匿名卷，验证后回收；不挂载家庭数据、Docker socket、摄像头或声卡。发布任务读取此前验收的同一份镜像，不重新构建；Git SHA、运行编号、尝试次数构成独立标签，digest 用于固定部署。只有当前主干才可推进 `main` 别名，流水线不会升级本机家庭部署。公开发布范围仍待确认，尚未完成 GHCR 发布验收。

本轮合并保留主干的新姿态复核及采集进程恢复逻辑。CPU 复核支持严格的可选 `review_prone` 参数，并按 Linux cgroup 的当前组与可见父组收紧内存准入，不能以宿主总内存绕过容器预算。Linux 条件性媒体 SDK 的包名按 `miloco-miot` 校验，macOS 原包名保持独立。

摄像头 SDK 的上游许可对用途有额外限制；本地安装和功能验收不代表取得公开再分发授权。自动发布输入不含该 SDK，亦不含模型权重或家庭账号。完整家庭镜像继续保留本地，不因 CI 工作替换。
