# Linux 云台 ABI 核验

目标只限官方 Miloco **2026.9.11 / Linux x86_64** 的 `libmiot_camera_lite.so`，文件 SHA-256 为 `fde805072a663d975da26e1b91836acad2da9c9e19038e382ad4118871c65ecc`。原始包与 wheel 的来源、摘要见 [Linux 运行时](linux-runtime.md)。本记录依据该文件的静态反汇编，不包含账号、设备或内存转储。

| 核验点 | ELF 虚拟地址 / 指令证据 |
| --- | --- |
| 导出锚点 `miot_camera_new` | `0x86de0` |
| SDK 停止流的既有发送调用 | `0x877a0` 读取 camera+0x38；`0x877a8` 传 opcode 0x103；`0x877ad` 调用 0x895a0 |
| 命令入口 | `0x895a0` 是保持四参数的跳板，`0x895cc` 跳到 0x8c220 |
| 参数语义 | `rdi=session`、`esi=opcode`、`rdx=payload`、`ecx=uint32 length`；0x8c257 限制长度最多 0x400 |
| 会话状态 | 0x8c2a7 读取 session+0，0x8c2b2 比较 connected=4 |
| 相机状态 | 导出 `miot_camera_status` 的 0x878b6 读取 camera+0x40 |
| 会话赋值与清理 | 0x875cd 保存 session 到 camera+0x38；0x87738 清空 |
| 文件偏移 | 代码 PT_LOAD 的 file offset 与 vaddr 都为 0x85000，故上述代码地址可直接索引固定文件 |

运行时先核对完整摘要和 ELF 架构，再用已加载导出锚点求装载基址，核对发送入口前 32 字节与固定文件一致。macOS 使用独立 Mach-O 配置，未套用到 Linux。

命令仍限原有已采用的 `0x112` 和 `{"operation":1..4}`，不开放任意 opcode 或用户地址。保留父子进程共用单调时钟的 1.5 秒有效期、0.7 秒最小间隔、连接状态、当前拥有者与既有跟踪授权门槛。模型跟踪只提供当前画面的方向建议，经原 `LocalFollower` / `CameraLease` 后才进入这个入口；SDK 回执不代表实物转动。

普通测试只用合成内存与回调验证方向、拒绝和生命周期，不加载 SDK 或控制摄像头。此为固定二进制的兼容适配，不是厂商公开稳定 ABI；更换版本须重新审查，拒绝自动寻找或猜测偏移。
