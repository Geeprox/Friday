# Friday KTV 电视端

## 软件更新

打开 Friday KTV 后，电视连上 NAS 就会在后台检查新版本。发现新版时，电视先下载并校验安装包。电视保持当前歌曲，等播放和加载结束、没有暂停待续的歌曲且应用处于前台时才安装。NAS 暂时离线时会自动重试。“房间设置 → 软件更新”也可以手动检查。

Android 可能要求允许 Friday KTV 安装应用，或显示确认安装画面。根据电视提示使用遥控器确认。更新完成后可从电视应用列表点开 Friday KTV，电视会接续有效房间。电视显示“Friday KTV 已更新”通知时，也可以通过通知打开。

如果下载中断，系统会在下一次运行时清理上次未完成的私人更新文件和会话。关闭安装确认后可以从“房间设置 → 软件更新”重新检查。发布端撤回安装包后，下一次检查即停止提供该版本。需要回退已装版本时，应使用保留的上一个可用源码和相同签名身份制作版本号更高的修复包，让 Android 保留应用数据。

## 歌曲背景

对于 3840×2160 的背景，电视会根据 Android 当前显示模式、实际绘制尺寸和可用内存选择解码尺寸。4K 输出且应用绘制面积高于 1080p 时可使用完整分辨率。若系统仍以 1080p 绘制应用，或内存不足，电视降采样显示同一张图。后台只解码当前要显示的一张背景，旧图和过期结果随播放进度释放。画面保持原图色彩，歌词用文字阴影保持可读性。实机原生 4K 显示还需该设备的 Android 绘制区域达到 3840×2160。 [Android 显示模式](https://developer.android.com/reference/android/view/Display#getMode()) · [Android 位图内存](https://developer.android.com/topic/performance/graphics/manage-memory)

## 构建和验收

电视端从已配置的家庭 NAS 获取 `GET /v1/tv/update`。发布清单包含版本号、版本名、文件长度、SHA-256 和固定文件路径 `/v1/tv/update.apk`。电视只安装比当前安装版本更高、包名和签名证书一致、字节长度和摘要匹配且提交安装前仍在发布的 APK。

用现有家庭 NAS 私有配置和已匹配该电视的签名构建。签名文件 `development-signing.p12`、`development-signing.password` 应位于当前 worktree 的 `.artifacts/ktv-tv/`。私有 NAS 配置通过以下参数指定。使用已有锁定的 Android SDK、JDK、NDK 和 Oboe，构建缓存由脚本放在隔离的 CodexBuilds 目录。

```sh
python3 -B scripts/build_ktv_tv.py --sdk "$KTV_ANDROID_SDK" --java "$KTV_JDK" \
  --ndk "$KTV_NDK" --oboe "$KTV_OBOE" --nas-connection-config "$KTV_NAS_PROFILE"
python3 -B scripts/install_ktv_tv.py --sdk "$KTV_ANDROID_SDK" --java "$KTV_JDK" \
  --main-only --audio-build-dir "$KTV_AUDIO_BUILD_DIR" --dry-run --no-launch
```

第一次验收先把带自更新功能的 0.72 主程序更新到电视，检查正常入房。验收自更新前，将 0.72 构建回执保留成 `.artifacts/ktv-tv/build-result-0.72.json` 并校对已留存的 APK SHA-256。下一轮用同一套私有配置、工具链和签名构建含 4K 背景支持的 0.73，执行离线预检并保留独立 APK 与回执。发布到家庭 NAS 后，在真实电视分别检查演唱时未触发安装、空闲后的系统结果、新版运行回报和 4K 背景绘制。

安装通过 Android `PackageInstaller` 会话进行。支持的系统版本会请求免确认的同签名自更新；设备安装权限、系统版本和厂商界面仍可能要求确认。系统发出 `ACTION_MY_PACKAGE_REPLACED` 后，应用可核对版本和清理收据；弹出应用界面仍受 Android 后台 Activity 规则限制。通知已获系统许可时可显示一个“打开”入口。电视端自更新无须启动 Mac 制备服务或依赖开发机在场。 [Android 安装行为](https://developer.android.com/reference/android/content/pm/PackageInstaller.SessionParams#setRequireUserAction(int)) · [应用替换通知](https://developer.android.com/reference/android/content/Intent#ACTION_MY_PACKAGE_REPLACED) · [后台 Activity 规则](https://developer.android.com/guide/components/activities/secure-bal)
