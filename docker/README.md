# CPU 镜像维护

本目录维护单一 amd64 镜像：Friday、ARL、CPU 视觉/语音和 KTV 运行环境同容器运行。Compose 与日常家庭迁移步骤由项目索引维护，见 [容器维护说明](https://github.com/lijunfeng722/friday-project-index/blob/main/docs/linux-container.md)。

本地候选：

```sh
python3 scripts/build_linux_container.py --arl ../ARL --tag friday-linux-cpu:candidate
python3 scripts/verify_linux_container.py --image friday-linux-cpu:candidate --camera-sdk --output .artifacts/container-verification.json
```

发布来源由 `runtime-lock.json` 固定 ARL 提交和全部运行文件摘要，CI 使用 `--release` 拒绝未提交或摘要不匹配的内容。更新锁必须先审查 ARL 改动、通过其回归并使该提交可供 CI 获取，不能将浮动分支或本机父提交写入发布记录。当前固定 ARL 提交已可从远端获取；GitHub Actions 与 GHCR 的远端发布结果仍需单独验收。

验收只创建本轮拥有的模拟容器；随机回环端口、匿名卷与家庭服务隔离，测试正常停止、重启和令牌轮换后回收。旧的针对当前家庭容器的 `--restart` 测试入口不再提供。

公开镜像不包含摄像头 SDK 本体。默认 `FRIDAY_CAMERA_SDK=auto`：首次启动从官方固定发行包下载 SDK，按 `camera-sdk-lock.json` 校验归档、wheel 和许可证，安装到私有数据卷 `data/camera-sdk/2026.9.11/`，成功后启动应用。后续启动先复核缓存，不再联网下载；应用启动前才切换到已验证的 SDK 环境。第三方 Python 依赖在镜像构建时按固定清单准备，首次启动不执行任意远端安装脚本。

首次启动预留 660 秒，日志依次显示 `downloading`、`installed_verified`；重启显示 `cached_verified`。下载失败最多重试三次，校验失败或缓存损坏会退出，不降级为假成功；临时目录在失败退出或下一轮启动时清理。网络恢复后重启即可重新下载未完成的安装。缓存损坏时先停止该候选容器，再备份并移走报错的版本目录，下一次启动重新取得固定版本；不要删除整个家庭数据卷。`camera-sdk-status` 子命令可复核安装。

纯控制面离线测试可显式设置 `FRIDAY_CAMERA_SDK=disabled` 跳过安装；该设置只适合不需要 SDK 的隔离环境。常规隔离验证默认跳过下载，追加 `--camera-sdk` 会实际下载、验证 SDK 可导入及重启缓存复用；CI 使用后者，仍不登录或连接摄像头。本地 `--camera-sdk` 构建入口继续支持既有 wheel，生成的本地镜像关闭重复下载。

项目仅用于非商业用途。原许可证保存在镜像 `/opt/container/licenses/Xiaomi-Miloco-LICENSE.md`，安装后另复制为缓存目录的 `LICENSE.md`，并保留 `NOTICE.txt`。SDK 适用原 Xiaomi Miloco License Agreement；非商业声明和自动下载不扩大原许可，Friday 集成用途的额外授权尚未确认。已有部署不由 CI 升级。Apple 专属服务继续标为不支持，Mini 文本播报仍属于设备合成的例外。
