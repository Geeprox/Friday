# Working on Friday

Read README.md, docs/deployment.md and docs/privacy.md before setup. Keep secrets,
household exports and generated reports under ignored data/ or .artifacts/.
Never request passwords, tokens, pairing codes or family exports in chat/commits.

## Deployment workflow

1. Run `python3 scripts/setup-dependencies.py plan`. Inspect OS, Python, uv, Docker,
   Node and already configured services. Reuse an existing HA instance.
2. Run `scripts/install-home.sh` and verify `bin/home doctor` and a Simulator
   snapshot. Simulator is the default and must remain visibly simulated.
3. For new HA, prepare with setup-dependencies.py. Container deployment requires
   Linux Docker Engine; macOS uses a Linux VM/remote HA. Configure timezone locally.
4. Install the pinned Xiaomi Home integration; complete account login in HA UI.
   Miloco is optional: download its pinned source with the helper, inspect official
   install instructions and use a supported agent platform. Do not invent a Codex
   or Docker installer for the current Miloco plugin architecture.
5. Discover rooms/entities/devices through HA registries. MIoT property IDs come
   from imported specs. Never identify a target by an author's model, IP, name or
   coordinates. Unknown/ambiguous capabilities must fail closed.
6. Import the user's Mi Home layout with the import script. Preview first; retain
   stable IDs, holes, missing positions and source coordinate basis. Never ship
   a captured home map, texture, compass direction or default household geometry.
7. Report each gate separately: dependency installed, service started, login,
   readback, requested control and physical user confirmation. Do not infer success
   from HTTP 200, ACKs, playing, decoded frame counts or synthetic tests.

## Development

Do not start cameras, microphones, models, audio, television or device actions as
part of ordinary tests. Optional hardware work needs scope from the user. Do not
expand a setup request into cloud inference or recurring device automation.

Use isolated, temporary paths for tests. Test receipts belong under .artifacts/.
Run the locked test environment and publication scanner before a release. Scan a
fresh exported clone as well as the source worktree. A clean current tree does
not sanitize old commits/branches/releases: keep private history private and
publish only reviewed clean history. Never push `--mirror` or `--all` to public.

## 公共开发约定与文档协作

本仓库保留以上部署、隐私、授权和发布约束，同时接入[公共开发规范](https://github.com/lijunfeng722/project-index/blob/main/docs/development.md)。核心约定由元仓库生成到本文件，专用约束在此维护。

开发时运行 `python3 scripts/check_dev_policy.py check`，再按实际改动选择 README.md 和 docs/development.md 中的验证入口。用户可见变化同步独立用户手册；安装器变化同步下载副本与校验清单；跨仓库部署入口变化同步元仓库。普通内部重构说明无用户文档影响即可。

<!-- friday-dev-policy:start -->
## Friday 公共开发约定

规范版本：2026-11-02.1

- 新增人工提交使用中文说明目的，推荐“功能：……”“修复：……”“文档：……”。保留英文技术名称；不为格式要求重写上游或已有历史。
- 新增、修改的解释性注释和文档字符串使用中文。保留工具指令、协议字段、标准术语、第三方原文及用于反例的测试数据；不要求批量翻译旧注释。
- 用户可见的新特性或使用方式变化，同步用户手册的步骤、限制与相关 FAQ；界面变化时更新对应产品截图。内部重构可说明无用户文档影响及理由。
- 架构、接口、数据流或部署方式变化，同步对应技术说明和必要的 Mermaid 图；涉及仓库职责、依赖和维护入口时同步顶级索引。
- 按变更选择相关验证，记录实际结果与未验证范围。代码提交、服务就绪、设备回报与使用者实物确认分别报告；普通测试使用隔离、合成输入。
- 保留原仓库的隐私与权限约束。凭据、真实家庭画面、私有配置、备份和未审阅的私有历史不进入公共仓库；只提交当前任务的文件。
- 提交、推送、发布、部署和真实设备动作按本次任务已有授权执行。只分析、只给方案、只读审查时遵守该边界；本规范和 Skill 不额外授予操作权限。
- 完整规则和跨仓库同步方式见 [Friday 开发规范](https://github.com/lijunfeng722/project-index/blob/main/docs/development.md)。
<!-- friday-dev-policy:end -->
