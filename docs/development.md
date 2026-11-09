# 开发与验证

先阅读 [AGENTS.md](../AGENTS.md)，核对当前任务的实现、验证与授权范围。公共约定在[元仓库开发规范](https://github.com/lijunfeng722/friday-project-index/blob/main/docs/development.md)统一维护，本仓库保留部署、隐私、设备发现与公开历史要求。

## 开发环境

使用锁定依赖和合成测试数据。回归环境使用 Python 3.12，包括固定的抽象语法树（AST）契约检查。创建环境：

```sh
uv sync --locked --python 3.12 --group dev --extra airplay --extra nas-connection --extra privacy-playback --extra ktv
```

完整测试套件运行前，使用 `npm --prefix workers/geek ci --ignore-scripts` 安装可选 Geek 工作进程的测试依赖；完整回归的依赖参数以 [CI 工作流](../.github/workflows/ci.yml)为准。基础控制台安装仍支持 Python 3.11 及以上版本。

完整回归包含 KTV 的真实 PNG 二维码生成，因此须选择已锁定的 `ktv` extra；
`qrcode` 本身不包含 `PyPNGImage` 所需的 PyPNG。无需为运行测试启动 KTV、登录、扬声器或电视。

## ARL 客户端与完整宿主测试

按[桌面构建说明](FRIDAY-DESKTOP-BUILD.md)显式准备固定公开 SDK 后，可在新测试进程中
使用 `PYTHONPATH=<该输入目录>` 和 `FRIDAY_TEST_ARL_SDK_INPUT=<该输入目录>` 运行客户端
及消费者回归。SDK 只参与合成协议与回执验证；CI 也使用同一固定清单，不要求开发者私有目录。
常规 Python 导入也必须设置 `PYTHONDONTWRITEBYTECODE=1`，避免在固定输入中生成
`__pycache__`；额外文件仍会使来源核验和清理拒绝，不能用忽略缓存的方式放宽清单。

CI 通过 `scripts/run_ci_regression.py` 为每个回归进程创建独占的 `/tmp/fci-*`，同时将它设为
`TMPDIR`，并把 pytest `--basetemp` 放在其 `p/` 子目录。这样 SQLite 仍遵守原本的
本机临时位置校验，Unix socket 也保留短路径余量；回归期间持有锁，结束后核对来源、
Git、类型和打开句柄再回收，保留外部小型 JSON 回执。引用不明时保留现场并使 job 失败。
默认 cacheprovider 保持启用，`cache_dir` 显式设为同一独占根的 `pytest-cache/` 并随之回收。
`RUNNER_TEMP` 只保留 SDK 输入和准备回执，不直接用作 pytest 数据库目录。
本机开发仍遵守项目缓存命名空间；设置 `TMPDIR` 时须指向本任务已创建的隔离目录，
需要短 socket 的测试才使用有来源记录、可回收的 `/tmp` 例外。

`tests/test_arl_priority_consumer.py` 覆盖真实公开 SDK 的 v2 协商、首次优先级提交、回执、
上下文撤销、取消与不重放。完整宿主的身份生产器、资源租约及代次替换由原有四个集成用例
单独覆盖；它们需要显式的 `FRIDAY_TEST_ARL_PRIORITY_HOST_SOURCE`，不能把公开客户端
缺少 `friday_profile` 或 `priority_host` 当作应扩充冻结 SDK 的理由。

完整宿主集成须在全新进程中把所选、已审阅的完整 SDK 根目录置于 `PYTHONPATH`，然后
设置上述变量运行 `tests/test_homeplane_arl_planning_priority.py`。未提供时明确跳过该
宿主范围；显式输入缺接口或与已导入 SDK 不同则失败，禁止替换已导入命名空间。
原生 sealed actor 用例也要求该明确来源及其候选；源 seal、身份和授权断言继续保留。
普通 CI 不提供私有宿主组合，不能将其跳过写成宿主或真实模型验收。

默认 API 测试分别注入无外部传输的会话或调用方持有的运行时，仍检查创建/启动/关闭
次数及 API、HA 订阅原有行为。线程异常视为错误，不吞掉 socket 越界尝试来消除警告。
这些是开发与验证入口变化，无终端用户功能、使用步骤或产品截图变化。

## 验证与隐私

首页状态的生命周期回归覆盖后台创建、首次读取、过期、返回前台、手动刷新及退出取消；后台定时轮询仍暂停。此修复恢复既有首页的状态显示，不新增用户操作步骤；当前渲染证据与真实家庭验收分别记录。

常规测试不创建真实家庭会话，也不运行真实模型或硬件。使用隔离临时目录，生成证据放在 `.artifacts/`，公开前分别检查源码工作区和独立导出的源码副本。

聚类挂起回归先等待合成子进程完成冷导入，再交给原有识别进程管理器运行；包含超过工作预算的
启动延迟样例，避免导入时间耗尽预算而误测其他分支。工作时限、停止与回收预算及未确认退出断言保持不变。

运行事件使用固定、有界的事件码，不记录凭据、原始转写、媒体、原始名称或异常正文。隐私测试应验证实际数据边界，不能只检查界面提示文字。

## 跨仓库交付

新增人工提交、解释性注释和协作规范正文使用中文，保留必要的文件名、命令与技术术语。运行 `python3 scripts/check_dev_policy.py check` 检查当前约定；有明确任务基线时用 `--base` 核对新提交，保留旧历史和上游信息。

新增用户可见能力或操作变化时同步用户手册的步骤、限制和 FAQ；架构、接口、授权或部署变化时更新对应技术说明和必要 Mermaid 图。涉及依赖安装器时同步用户手册的下载副本与 SHA256 清单，涉及跨仓库维护入口时更新元仓库。纯内部变更说明无用户文档影响及理由。

PR 模板记录三类文档影响，检查工作流验证固定版本的规则和新增人工提交；注释质量、文档覆盖与真实设备效果仍由审查和对应实测确认。
