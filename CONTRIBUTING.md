# Contributing

Use synthetic fixtures. Keep household-specific configuration, device models,
serials, network addresses and geometry out of implementation decisions. Extend
capability discovery and test at least two different registries/spec mappings.

Install the locked development environment and run the commands in README.md.
Include regression tests for privacy, authority and device-routing changes. Put
all generated evidence in .artifacts/. Run scripts/audit_publication.py before
submitting; do not paste its operator-local private-pattern file into a PR.

## 开发约定与文档影响

同时遵循 [AGENTS.md](AGENTS.md) 中的公共约定与[跨仓库开发规范](https://github.com/lijunfeng722/project-index/blob/main/docs/development.md)。新增人工提交、解释性注释和文档字符串使用中文，保留必要的技术名称及工具、协议与第三方原文。

运行 `python3 scripts/check_dev_policy.py check`，在 PR 中分别说明用户文档、技术文档和顶级索引的影响及依据。用户可见变化同步独立用户手册，界面变化更新对应示例截图；普通内部修改可以说明无用户文档影响。产品代码提交不代表已经部署或实物验收。
