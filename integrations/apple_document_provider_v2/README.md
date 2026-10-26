# Apple 本地文档 provider 独立源码包

PDFKit 真实文字层提取通过合成测试；Vision 源码已编译、真实调用失败，**OCR 资格 blocked**。
冻结 v2 的有限协议／来源／PDFKit／取消测试为 **26 passed**；它们不代表 OCR 成功或生产采用。

冻结 v2 的 owned root：`/path/to/private-work/2026-10-02/task-18/apple-document-provider`。
原仓库与主 owner 的 queue/config/API/shared files 均未写入。第一 native 槽由 root 分配并已释放。
无常驻进程、服务、安装、系统授权、NAS 访问、私人文件读取或云上传。

本修复仅在 task-26 的隔离副本修改 Python wrapper／文档，原 ZIP 保持冻结。仅运行 Python
receipt 回放与协议 double 回归；下列 26 tests、编译和 native evidence 均为历史证据，本轮未重跑。
source 唯一 owner 为 `01a0fc0b`；新 rich provider 须经独立复核及 owner checkpoint 后再集成。

## 可审源码与准确接口

- `native/PDFKitVisionPageBridge.swift` 是唯一 Apple native 桥：stdin 有限 bytes / 单页请求，stdout Native wire v2。
  主 owner 的旧 Swift 是已撤下的未提交草稿，当前并不存在；本包没有第二套并行 bridge。
- `apple_document_provider.py::AppleNativeBridge.page(document,page,max_chars)` 返回主 owner 的
  `StewardPDFPageReceipt`，兼容其 embedded-only `PDFTextProvider`。
- `AppleNativeBridge.extract(...)` 输出 `StewardNativeTextReceipt`；默认 mode=`pdf_text`，
  `pdf_auto` 做逐页文字质量及 raster 检查，`image_ocr` 输出图片结果。
- `ApplePDFProvider.step(job,budget)` 复用既有 `ExtractionStep`，每次一页，outcome_version=2；
  full SHA-256、source.current 前后复核、1-based 页码、旧 `pdf-page-N` cursor、真实 engine/provenance。
  limit/failure/low_quality 抛 `AppleProviderError` 并保留 receipt，**不推进 cursor**。
  消费边界重验 typed receipt、本次字符预算、页号与路由；`pdf_text` 拒绝 Vision 回执。
  `method=none` 或错路由没有可信 PageOutcome；保留 typed receipt，禁止补造 engine/provenance。
  主队列须由其 owner 串行采用；本 provider 不写索引、SQLite、queue 或 configuration。
- `AppleImageProvider.extract_image(job,budget)` 返回独立 `AppleImageObservation(anchor,receipt,unit)`。
  anchor 为整个原图的 top-left normalized `(0,0,1,1)`，无虚构 PDF page_count；字块定位未验证。
  图片 `step` 明确拒绝，避免塞进主 owner 的 PDF-page-only PageOutcome。

准确 Native request/receipt、PageOutcome v2 与 provenance/quality 来自 `protocol-snapshot/` 中
主 owner 源码及 JSON schema，10 个文件有 SHA-256。当前复核全部匹配；最终集成前 root 仍须
核对主 owner 确认的 checkpoint/hash。开发 qualification 布尔值不能授予可信 scope。

Native request 为 document_base64/page/max_chars + request_version=2/mode/max_pages/languages/max_pixels/seconds；
receipt 为 page/page_count/status/text/method/engine_version/quality/languages_used/languages_supported/reason。
语言表保留全部运行时结果，上限 64；本机返回 33 种。languages_used 是配置的识别语言，
不伪称文字实际检测语言。文字质量标 unassessed/review_required；OCR raw confidence 不是校准准确率。

## 冻结 v2 的历史执行证据

Apple Swift 6.4、arm64 macOS 27.0.1；顺序 swiftc 编译，无包或模型下载，无整个 cache 复制。
`scripts/build.sh` 编译成功后才原子替换 binary，`evidence/build.json` 固定最终 source/binary SHA-256。

- CoreGraphics/CoreText 生成英／中文 PNG、四页 text/scan/blank/mixed PDF、中文扫描 PDF、poor-text、257 页、malformed fixtures。
  CoreGraphics 核页数／PNG dimensions；6 个代表页均已渲染并视觉检查。
- PDFKit 提取真实 74 字符文字层，page=1/page_count=4，兼容旧 Python provider。
- 图片/扫描页/混合页的 Vision 尝试均返回 failed 与空 text；最终 6-case 资格 **1 passed / 5 blocked**。
  `evidence/native-smoke-final.json` 含实际 receipts、耗时、完整语言表和累计 child peak RSS。
- 桥外最小 `VNRecognizeTextRequest` 在默认 compute device 下 revision 3 和 2 也都失败：
  `Foundation._GenericObjCError` / code 0。0.63 秒、child peak RSS 37,060,608 bytes，
  见 `evidence/vision-minimal-probe.json`。冻结桥的五个回执均为 `vision_perform_failed`，记录相同错误。
  具体环境原因未知；未通过扩大权限、系统授权或下载资产绕过。
- `tests/test_provider.py`：26 passed，含真实 PDFKit、页数／字符／像素限额、source budget/hash/revocation、
  grant-after-read/after-result fencing、旧 seam、v2 出处、续页、失败 cursor 与 byte 上限。
  timeout/cancel 的 kill/reap 使用明确声明的 slow process double，观察到 exit=-9；double 不生成假 OCR。
- Ruff check/format、Python syntax、shell syntax 通过。测试后仅格式化和独立 import 排序，
  函数／类 AST 与通过版本一致，证据见 `python-format-validation.json`。

## 未通过／未运行

Vision OCR 成功、中文准确率、真正的 OCR empty 成功回执、low-confidence 成功路径均**未验收**。
不把 failed 改成 empty，不填演示 text，不标生产 ready。locked/restricted、EXIF 旋转、多帧图像、
复杂版面／列顺序、inline image/masks、恶意文档及 kernel 内存隔离未完成独立资格。
384 MiB peak-RSS watchdog 是 20 ms 抽样监测，不是内核硬限制；超限 kill 分支未用大分配实测。
没有生产 source grant/native sandbox/slot scheduler/cumulative reservation composition；
没有敏感目录索引、OCR PDF 层写回、真实 NAS、私人输入、physical file operations 或队列集成运行。

## 复现

先取得 root 的 native slot。使用已有 Friday Python 环境，读取主 owner checkout，仅向此 owned root 写证据。

```sh
sh scripts/build.sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:/path/to/Friday-file-steward /path/to/existing/python -m pytest -q -p no:cacheprovider tests/test_provider.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:/path/to/Friday-file-steward /path/to/existing/python scripts/native_smoke.py
```

`scripts/report_qualification.py` 只核保存的实际 receipts；当前 exit=2 表示 OCR blocked。
Fixtures 已附带；fixtures 下的 Swift generators/probes 重新运行也须遵守 slot 和合成资料边界。
缺少 source/bridge/native_qualified 或 executable hash 时默认拒绝。

参照 Apple [PDFPage](https://developer.apple.com/documentation/pdfkit/pdfpage)、
[VNRecognizeTextRequest](https://developer.apple.com/documentation/vision/vnrecognizetextrequest)、
[supportedRecognitionLanguages](https://developer.apple.com/documentation/vision/vnrecognizetextrequest/supportedrecognitionlanguages())；
计算设备签名核本机 Vision.swiftinterface/VNRequest.h。语言支持 API 不证明 perform 成功。
