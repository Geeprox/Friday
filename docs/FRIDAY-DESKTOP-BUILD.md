# 本机桌面构建

需要 macOS 14+、Xcode Command Line Tools，以及已安装 Friday 基础依赖的 Python 环境。
构建从当前源代码开始，不复用作者机器的原生二进制、签名证书或历史资格文件。

```sh
./scripts/install-home.sh
native/FridayDesktop/script/build_and_run.sh --plan
native/FridayDesktop/script/build_and_run.sh --build-only
```

默认产物：`.artifacts/desktop-public/Friday.app`。默认 ad-hoc 签名；可通过
`--signing-identity` 选择自己的签名身份。构建不安装、不启动、不打开设备。

当前是**本机开发包**：内置 launcher 引用构建时明确选择的本机 Python 环境。
移动或删除该环境后需要重新构建，不能把此包作为自包含正式安装包转发。
正式可分发包仍需独立的 Python runtime 打包、签名、notarization 和洁净主机验收。

显式安装使用 `native/FridayDesktop/script/install_desktop.py --candidate <app-path>`。
已有 App 需先停止并指定 `--backup`；默认不会覆盖。当前用户的私有配置保存在
`~/Library/Application Support/Friday`，从本机路径推导，不读取作者环境。
