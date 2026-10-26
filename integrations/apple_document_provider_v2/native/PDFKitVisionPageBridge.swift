// The single Apple bridge successor, owned by task-18. Bytes-only, one page per process.
// Wire: main owner's StewardNativeTextRequest / Receipt v2. No file paths or PDF writes.
import Foundation
import PDFKit
import Vision
import CoreML
import CoreGraphics
import ImageIO
import Darwin

let sourceByteLimit = 8_388_608
let wireByteLimit = 11_188_000
let peakRSSLimit = 384 * 1_048_576

struct NativeRequest: Decodable {
    let schema_version: Int?
    let request_version: Int?
    let document_base64: String
    let page: Int
    let max_chars: Int
    let mode: String?
    let max_pages: Int?
    let languages: [String]?
    let max_pixels: Int?
    let seconds: Double?
}

struct NativeQuality: Encodable {
    var embedded_chars = 0
    var meaningful_chars = 0
    var flags: [String] = []
    var ocr_min_confidence: Float? = nil

    mutating func flag(_ value: String) {
        if !flags.contains(value), flags.count < 8 { flags.append(value) }
    }
}

struct NativeReceipt: Encodable {
    let receipt_version = 2
    var page: Int
    var page_count: Int? = nil
    var status: String
    var text = ""
    var method = "none"
    var engine_version: String
    var quality = NativeQuality()
    var languages_used: [String] = []
    var languages_supported: [String] = []
    var reason: String
}

let os = ProcessInfo.processInfo.operatingSystemVersion
let enginePrefix = "apple_native_v2:macOS:\(os.majorVersion).\(os.minorVersion).\(os.patchVersion)"

func failure(_ status: String, page: Int = 1, count: Int? = nil,
             method: String = "none", reason: String? = nil) -> NativeReceipt {
    NativeReceipt(page: page, page_count: count, status: status, method: method,
                  engine_version: enginePrefix + ":" + method, reason: reason ?? status)
}

func scalarCount(_ text: String) -> Int { text.unicodeScalars.count }
func meaningfulCount(_ text: String) -> Int {
    text.unicodeScalars.filter { CharacterSet.alphanumerics.contains($0) }.count
}

func assessEmbedded(_ text: String, characters: Int) -> NativeQuality {
    var value = NativeQuality(embedded_chars: min(1_000_000, max(0, characters)),
                              meaningful_chars: meaningfulCount(text))
    let invalid = text.unicodeScalars.filter {
        $0.value == 0xfffd || ($0.value < 32 && ![9, 10, 13].contains($0.value))
    }.count
    if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { value.flag("embedded_no_text") }
    else if value.meaningful_chars < 8 { value.flag("sparse_embedded_text") }
    if invalid > 0 && Double(invalid) / Double(max(1, scalarCount(text))) > 0.02 {
        value.flag("suspect_text_mapping")
    }
    value.flag("extraction_accuracy_unassessed")
    return value
}

// Inspect only bounded XObject dictionaries, including Forms. A raster plus a text title
// must not make pdf_auto skip the scanned content. Inline/unusual graphics remain unverified.
final class RasterInspection {
    var nodes = 0
    var hasRaster = false
    var tooLarge = false
    var incomplete = false
    let maxPixels: Int
    init(maxPixels: Int) { self.maxPixels = maxPixels }
}

final class RasterFrame {
    let state: RasterInspection
    let depth: Int
    init(_ state: RasterInspection, _ depth: Int) { self.state = state; self.depth = depth }
}

func scanResources(_ resources: CGPDFDictionaryRef, _ state: RasterInspection, depth: Int) {
    guard depth <= 4, state.nodes <= 128 else { state.incomplete = true; return }
    var objects: CGPDFDictionaryRef?
    guard CGPDFDictionaryGetDictionary(resources, "XObject", &objects), let objects else { return }
    guard CGPDFDictionaryGetCount(objects) <= 128 else { state.incomplete = true; return }
    let frame = RasterFrame(state, depth)
    let pointer = Unmanaged.passUnretained(frame).toOpaque()
    CGPDFDictionaryApplyFunction(objects, { _, object, info in
        guard let info else { return }
        let frame = Unmanaged<RasterFrame>.fromOpaque(info).takeUnretainedValue()
        frame.state.nodes += 1
        guard frame.state.nodes <= 128 else { frame.state.incomplete = true; return }
        var stream: CGPDFStreamRef?
        guard CGPDFObjectGetValue(object, .stream, &stream), let stream,
              let dictionary = CGPDFStreamGetDictionary(stream) else { return }
        var subtype: UnsafePointer<CChar>?
        guard CGPDFDictionaryGetName(dictionary, "Subtype", &subtype), let subtype else { return }
        switch String(cString: subtype) {
        case "Image":
            frame.state.hasRaster = true
            var width: CGPDFInteger = 0
            var height: CGPDFInteger = 0
            if !CGPDFDictionaryGetInteger(dictionary, "Width", &width)
                || !CGPDFDictionaryGetInteger(dictionary, "Height", &height)
                || width <= 0 || height <= 0 || width > frame.state.maxPixels
                || height > frame.state.maxPixels || width > frame.state.maxPixels / max(1, height) {
                frame.state.tooLarge = true
            }
        case "Form":
            var nested: CGPDFDictionaryRef?
            if CGPDFDictionaryGetDictionary(dictionary, "Resources", &nested), let nested {
                scanResources(nested, frame.state, depth: frame.depth + 1)
            }
        default: break
        }
    }, pointer)
}

func inspectRaster(_ page: CGPDFPage, maxPixels: Int) -> RasterInspection {
    let state = RasterInspection(maxPixels: maxPixels)
    var dictionary: CGPDFDictionaryRef? = page.dictionary
    for _ in 0..<16 {
        guard let current = dictionary else { return state }
        var resources: CGPDFDictionaryRef?
        if CGPDFDictionaryGetDictionary(current, "Resources", &resources), let resources {
            scanResources(resources, state, depth: 0)
            return state
        }
        var parent: CGPDFDictionaryRef?
        if !CGPDFDictionaryGetDictionary(current, "Parent", &parent) { return state }
        dictionary = parent
    }
    state.incomplete = true
    return state
}

func render(_ page: CGPDFPage, maxPixels: Int) -> CGImage? {
    let box = page.getBoxRect(.cropBox)
    guard box.width.isFinite, box.height.isFinite, box.width > 0, box.height > 0 else { return nil }
    let rotation = abs(page.rotationAngle) % 180
    let pageWidth = rotation == 90 ? box.height : box.width
    let pageHeight = rotation == 90 ? box.width : box.height
    let scale = min(2048 / max(pageWidth, pageHeight),
                    sqrt(CGFloat(maxPixels) / pageWidth / pageHeight))
    let width = Int(floor(pageWidth * scale))
    let height = Int(floor(pageHeight * scale))
    guard width >= 1, height >= 1, width <= 2048, height <= 2048,
          width <= maxPixels / height,
          let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                                  bytesPerRow: width * 4, space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return nil }
    let target = CGRect(x: 0, y: 0, width: width, height: height)
    context.setFillColor(CGColor(gray: 1, alpha: 1))
    context.fill(target)
    context.concatenate(page.getDrawingTransform(.cropBox, rect: target, rotate: 0,
                                                   preserveAspectRatio: true))
    context.drawPDFPage(page)
    return context.makeImage()
}

func recognize(_ image: CGImage, orientation: CGImagePropertyOrientation = .up,
               request input: NativeRequest, count: Int?, method: String,
               quality initialQuality: NativeQuality) -> NativeReceipt {
    var receipt = failure("failed", page: input.page, count: count, method: method, reason: "vision_failed")
    receipt.quality = initialQuality
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    request.preferBackgroundProcessing = true
    // System built-in CPU implementation only; no external models or downloads.
    request.automaticallyDetectsLanguage = false
    var failurePhase = "vision_language_query_failed"
    do {
        let available = try request.supportedRecognitionLanguages().sorted()
        guard available.count <= 64 else {
            receipt.status = "unsupported"; receipt.reason = "supported_languages_limit"; return receipt
        }
        receipt.languages_supported = available
        let selected = (input.languages?.isEmpty == false) ? input.languages! :
            ["zh-Hans", "zh-Hant", "en-US"].filter { available.contains($0) }
        guard !selected.isEmpty, selected.allSatisfy({ available.contains($0) }) else {
            receipt.status = "unsupported"; receipt.reason = "recognition_language_unavailable"; return receipt
        }
        request.recognitionLanguages = selected
        receipt.languages_used = selected
        receipt.engine_version = enginePrefix + ":vision_revision_\(request.revision):cpu"
        failurePhase = "vision_compute_query_failed"
        let stages = try request.supportedComputeStageDevices
        for (stage, devices) in stages {
            guard let cpu = devices.first(where: { if case .cpu = $0 { return true }; return false }) else {
                receipt.status = "unsupported"; receipt.reason = "vision_cpu_unavailable"; return receipt
            }
            request.setComputeDevice(cpu, for: stage)
        }
        failurePhase = "vision_perform_failed"
        try VNImageRequestHandler(cgImage: image, orientation: orientation).perform([request])
        let observations = request.results ?? []
        guard observations.count <= 512 else {
            receipt.status = "limit"; receipt.reason = "ocr_observation_limit"; return receipt
        }
        // Full-page reading order heuristic; no column/layout fidelity assertion.
        let ordered = observations.sorted {
            if $0.boundingBox.midY != $1.boundingBox.midY { return $0.boundingBox.midY > $1.boundingBox.midY }
            return $0.boundingBox.minX < $1.boundingBox.minX
        }
        let candidates = ordered.compactMap { $0.topCandidates(1).first }
        let text = candidates.map(\.string).joined(separator: "\n")
        receipt.quality.flag("layout_order_unverified")
        guard scalarCount(text) <= input.max_chars else {
            receipt.status = "limit"; receipt.reason = "ocr_text_limit"; return receipt
        }
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            receipt.status = "empty"; receipt.reason = "ocr_no_text"
            receipt.quality.meaningful_chars = 0
            receipt.quality.flag("no_text_not_proof_of_blank")
            return receipt
        }
        receipt.text = text
        receipt.quality.meaningful_chars = meaningfulCount(text)
        receipt.quality.ocr_min_confidence = candidates.map(\.confidence).min()
        if (receipt.quality.ocr_min_confidence ?? 0) < 0.65 || receipt.quality.meaningful_chars == 0 {
            receipt.status = "low_quality"; receipt.reason = "ocr_review_required"
            receipt.quality.flag("low_ocr_confidence")
        } else {
            receipt.status = "ocr_text"; receipt.reason = "ocr_text"
        }
        return receipt
    } catch {
        // Framework exception details may contain sensitive material; expose only stable codes.
        let nativeError = error as NSError
        receipt.reason = failurePhase
        receipt.quality.flag("vision_error_\(nativeError.code)")
        if nativeError.domain.range(of: "^[A-Za-z0-9_.:-]{1,120}$", options: .regularExpression) != nil {
            receipt.quality.flag("error_domain_" + nativeError.domain)
        }
        return receipt
    }
}

func extract(_ input: NativeRequest, bytes: Data) -> NativeReceipt {
    let mode = input.mode ?? "pdf_text"
    let maxPixels = input.max_pixels ?? 4_000_000
    if mode == "image_ocr" {
        guard input.page == 1 else { return failure("missing", page: input.page, reason: "image_page_must_be_one") }
        guard let source = CGImageSourceCreateWithData(bytes as CFData,
            [kCGImageSourceShouldCache: false] as CFDictionary) else {
            return failure("malformed", page: input.page, method: "vision_image")
        }
        guard CGImageSourceGetCount(source) > 0 else {
            return failure("malformed", page: input.page, method: "vision_image")
        }
        guard CGImageSourceGetCount(source) == 1 else {
            return failure("unsupported", page: input.page, method: "vision_image", reason: "multiple_image_frames")
        }
        guard let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int,
              let height = properties[kCGImagePropertyPixelHeight] as? Int,
              width > 0, height > 0, width <= maxPixels, height <= maxPixels,
              width <= maxPixels / height else {
            return failure("limit", page: input.page, method: "vision_image", reason: "image_pixel_limit")
        }
        guard let image = CGImageSourceCreateImageAtIndex(source, 0,
            [kCGImageSourceShouldCacheImmediately: true] as CFDictionary) else {
            return failure("malformed", page: input.page, method: "vision_image")
        }
        let rawOrientation = (properties[kCGImagePropertyOrientation] as? UInt32) ?? 1
        guard let orientation = CGImagePropertyOrientation(rawValue: rawOrientation) else {
            return failure("malformed", page: input.page, method: "vision_image", reason: "invalid_image_orientation")
        }
        var quality = NativeQuality()
        quality.flag("extraction_accuracy_unassessed")
        return recognize(image, orientation: orientation, request: input, count: nil,
                         method: "vision_image", quality: quality)
    }
    guard let document = PDFDocument(data: bytes) else { return failure("malformed", page: input.page) }
    guard !document.isLocked else { return failure("locked", page: input.page) }
    guard document.allowsCopying else { return failure("restricted", page: input.page) }
    let count = document.pageCount
    guard count > 0, count <= (input.max_pages ?? 256) else {
        return failure("limit", page: input.page, count: count > 0 && count <= 100_000 ? count : nil,
                       method: "pdfkit", reason: "pdf_page_limit")
    }
    guard input.page <= count, let page = document.page(at: input.page - 1) else {
        return failure("missing", page: input.page, count: count, method: "pdfkit")
    }
    guard page.numberOfCharacters <= input.max_chars else {
        return failure("limit", page: input.page, count: count, method: "pdfkit", reason: "embedded_text_limit")
    }
    let text = page.string ?? ""
    guard scalarCount(text) <= input.max_chars else {
        return failure("limit", page: input.page, count: count, method: "pdfkit", reason: "embedded_text_limit")
    }
    var quality = assessEmbedded(text, characters: page.numberOfCharacters)
    let suspect = quality.flags.contains("embedded_no_text") || quality.flags.contains("sparse_embedded_text")
        || quality.flags.contains("suspect_text_mapping")
    guard let cgPage = page.pageRef else {
        return failure("malformed", page: input.page, count: count, method: "pdfkit", reason: "pdf_page_ref_missing")
    }
    let raster = inspectRaster(cgPage, maxPixels: maxPixels)
    if raster.hasRaster { quality.flag("raster_present") }
    if raster.incomplete { quality.flag("raster_inspection_incomplete") }
    quality.flag("graphics_coverage_unverified")
    if mode == "pdf_text" {
        var receipt = failure(suspect ? "needs_ocr" : "text", page: input.page, count: count,
                              method: "pdfkit", reason: suspect ? "embedded_quality_requires_ocr" : "embedded_text")
        receipt.quality = quality
        if !suspect { receipt.text = text }
        return receipt
    }
    if !suspect && !raster.hasRaster && !raster.incomplete {
        var receipt = failure("text", page: input.page, count: count, method: "pdfkit", reason: "embedded_text")
        receipt.quality = quality; receipt.text = text
        return receipt
    }
    guard !raster.tooLarge else {
        return failure("limit", page: input.page, count: count, method: "vision_pdf", reason: "pdf_raster_pixel_limit")
    }
    guard let image = render(cgPage, maxPixels: maxPixels) else {
        return failure("limit", page: input.page, count: count, method: "vision_pdf", reason: "pdf_render_limit")
    }
    // Replace the page text with a full-page OCR observation. Never merge duplicate text layers.
    return recognize(image, request: input, count: count, method: "vision_pdf", quality: quality)
}

func readInput() -> Data? {
    var input = Data()
    do {
        while let part = try FileHandle.standardInput.read(upToCount: 65_536), !part.isEmpty {
            guard input.count + part.count <= wireByteLimit else { return nil }
            input.append(part)
        }
        return input
    } catch { return nil }
}

func run() -> NativeReceipt {
    let allowedKeys: Set<String> = ["schema_version", "request_version", "document_base64", "page", "max_chars",
                                    "mode", "max_pages", "languages", "max_pixels", "seconds"]
    guard let input = readInput(),
          let object = (try? JSONSerialization.jsonObject(with: input)) as? [String: Any],
          Set(object.keys).isSubset(of: allowedKeys),
          let request = try? JSONDecoder().decode(NativeRequest.self, from: input) else { return failure("malformed") }
    let seconds = request.seconds ?? 5
    guard (request.schema_version ?? 1) == 1, (request.request_version ?? 2) == 2,
          (1...256).contains(request.page), (1...4000).contains(request.max_chars),
          ["pdf_text", "pdf_auto", "image_ocr"].contains(request.mode ?? "pdf_text"),
          (1...256).contains(request.max_pages ?? 256),
          (1...4_000_000).contains(request.max_pixels ?? 4_000_000),
          (request.languages?.count ?? 0) <= 8,
          (request.languages ?? []).allSatisfy({ $0.range(of: "^[A-Za-z0-9_.:-]{1,160}$", options: .regularExpression) != nil }),
          seconds.isFinite, seconds > 0, seconds <= 5,
          request.document_base64.utf8.count <= 11_184_812,
          let bytes = Data(base64Encoded: request.document_base64), !bytes.isEmpty,
          bytes.count <= sourceByteLimit else {
        return failure("limit", page: (1...256).contains(request.page) ? request.page : 1, reason: "request_limit")
    }
    let started = DispatchTime.now().uptimeNanoseconds
    let watchdog = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
    watchdog.schedule(deadline: .now(), repeating: .milliseconds(20))
    watchdog.setEventHandler {
        if Double(DispatchTime.now().uptimeNanoseconds - started) / 1_000_000_000 >= seconds { _exit(124) }
        var usage = rusage()
        if getrusage(RUSAGE_SELF, &usage) == 0 && usage.ru_maxrss > peakRSSLimit { _exit(125) }
    }
    watchdog.resume()
    defer { watchdog.cancel() }
    return autoreleasepool { extract(request, bytes: bytes) }
}

let receipt = run()
if let bytes = try? JSONEncoder().encode(receipt), bytes.count <= 65_536 {
    FileHandle.standardOutput.write(bytes)
} else {
    FileHandle.standardOutput.write(try! JSONEncoder().encode(failure("limit", reason: "output_limit")))
}
