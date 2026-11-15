# CPU 镜像维护

本目录维护单一 amd64 镜像：Friday、ARL、CPU 视觉/语音和 KTV 运行环境同容器运行。Compose 与日常家庭迁移步骤由项目索引维护，见 [容器维护说明](https://github.com/lijunfeng722/friday-project-index/blob/main/docs/linux-container.md)。

本地候选：

```sh
python3 scripts/build_linux_container.py --arl ../ARL --tag friday-linux-cpu:candidate
python3 scripts/verify_linux_container.py --image friday-linux-cpu:candidate --output .artifacts/container-verification.json
```

发布来源由 `runtime-lock.json` 固定 ARL 提交和全部运行文件摘要，CI 使用 `--release` 拒绝未提交或摘要不匹配的内容。更新锁必须先审查 ARL 改动、通过其回归并使该提交可供 CI 获取，不能将浮动分支或本机父提交写入发布记录。当前锁处于集成审查阶段，尚未可发布。

验收只创建本轮拥有的模拟容器；随机回环端口、匿名卷与家庭服务隔离，测试正常停止、重启和令牌轮换后回收。旧的针对当前家庭容器的 `--restart` 测试入口不再提供。

公开发布不打包条件性摄像头 SDK。本地 `--camera-sdk` 仅接受原固定 wheel 和摘要；使用者须自行确认许可范围，它不是自动下载或公开分发授权。已有部署不由 CI 升级。Apple 专属服务继续标为不支持，Mini 文本播报仍属于设备合成的例外。
