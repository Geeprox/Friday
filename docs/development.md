# Development

Read AGENTS.md. Use locked dependencies and synthetic fixtures. Do not create a
new household session or run models/hardware for ordinary test verification.

Runtime events use fixed, bounded codes and do not log secrets, raw transcripts,
media, names or exception bodies. Put generated receipts under .artifacts/.
Privacy tests must verify actual data boundaries, not only UI wording.

The regression environment is Python 3.12 (including AST contract pins). Create it
with `uv sync --locked --python 3.12 --group dev --extra airplay --extra nas-connection`.
Install the optional Geek worker test dependencies with
`npm --prefix workers/geek ci --ignore-scripts` before the complete suite.
The basic Console installation still supports Python 3.11+.

## 中文约定与跨仓库交付

公共约定在[元仓库开发规范](https://github.com/lijunfeng722/project-index/blob/main/docs/development.md)统一维护，本仓库的 AGENTS.md 包含核心副本。运行 `python3 scripts/check_dev_policy.py check` 检查当前约定；有明确任务基线时用 `--base` 核对新提交，保留旧历史和上游信息。

新增用户可见能力或操作变化时同步用户手册的步骤、限制和 FAQ；架构、接口、授权或部署变化时更新对应技术说明和必要 Mermaid 图。涉及依赖安装器时同步用户手册的下载副本与 SHA256 清单，涉及跨仓库维护入口时更新元仓库。纯内部变更说明无用户文档影响及理由。

PR 模板记录三类文档影响，检查工作流验证固定版本的规则和新增人工提交；注释质量、文档覆盖与真实设备效果仍由审查和对应实测确认。
