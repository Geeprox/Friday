# 参与 Friday 开发

先阅读 [AGENTS.md](AGENTS.md) 和[开发说明](docs/development.md)，按[跨仓库开发规范](https://github.com/lijunfeng722/friday-project-index/blob/main/docs/development.md)使用中文提交说明、解释性注释和协作规范正文。

## 实现与验证

使用合成测试数据，不把特定家庭的配置、设备型号、序列号、网络地址或几何数据作为实现依据。扩展能力发现时，至少验证两种不同的注册表或规格映射。

安装锁定依赖的开发环境，运行 README.md 中的验证命令。隐私、控制权限和设备路由变化必须包含回归测试。生成证据放在 `.artifacts/`；提交前运行 `python3 scripts/audit_publication.py`，不要将仅供本机使用的私有匹配词文件粘贴到 PR。

## 提交与文档影响

运行 `python3 scripts/check_dev_policy.py check`，在 PR 中分别说明用户文档、技术文档和顶级索引的影响及依据。用户可见变化同步独立用户手册，界面变化更新对应示例截图；内部修改说明无用户文档影响及理由。

沿用 AGENTS.md 中的授权和公开历史要求，分别说明代码提交、实际部署和用户实物验收结果。
