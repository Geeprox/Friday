// Optional camera-PCM classifier. No microphone, file audio, network or speech API.
// SoundAnalysis model loading requires the explicit --camera-pcm process mode.
import Foundation
import AVFoundation
import SoundAnalysis
import CoreMedia
import CoreFoundation
import Darwin

func emit(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([10]))
    }
}

guard CommandLine.arguments.dropFirst() == ["--camera-pcm"] else {
    emit(["type": "unavailable", "version": 1, "code": "explicit_camera_pcm_required"])
    exit(64)
}
guard let owner = ProcessInfo.processInfo.environment["FRIDAY_SOUND_OWNER_PID"].flatMap(Int32.init),
      owner == getppid() else { exit(64) }
let ownerWatch = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
ownerWatch.schedule(deadline: .now()+1, repeating: 1)
ownerWatch.setEventHandler { if getppid() != owner { exit(0) } }
ownerWatch.resume()
Thread.current.qualityOfService = .utility

// Bound the private pipe even if a caller sends an invalid oversized line.
var pending = Data()
func packet() throws -> Data? {
    while true {
        if let end = pending.firstIndex(of: 10) {
            let line = pending[..<end]
            pending.removeSubrange(...end)
            return Data(line)
        }
        guard pending.count <= 100_000 else { throw ProtocolFault.invalid }
        guard let part = try FileHandle.standardInput.read(upToCount: 4096), !part.isEmpty else {
            return nil
        }
        pending.append(part)
    }
}
enum ProtocolFault: Error { case invalid, unavailable }

final class Results: NSObject, SNResultsObserving {
    let identifiers: Set<String>
    let lock = NSLock()
    let done = DispatchSemaphore(value: 0)
    var windows: [[String: Any]] = []
    var failed = false
    init(_ identifiers: Set<String>) { self.identifiers = identifiers }
    func request(_ request: SNRequest, didProduce result: SNResult) {
        guard let result = result as? SNClassificationResult else { return }
        var scores = Dictionary(uniqueKeysWithValues: identifiers.map { ($0, 0.0) })
        var competing = 0.0
        for item in result.classifications {
            if identifiers.contains(item.identifier) { scores[item.identifier] = item.confidence }
            else { competing = max(competing, item.confidence) }
        }
        lock.lock();defer { lock.unlock() }
        guard windows.count < 4 else { failed = true;return }
        windows.append(["offset": result.timeRange.start.seconds,
                        "duration": result.timeRange.duration.seconds,
                        "scores": scores, "competing_confidence": competing])
    }
    func request(_ request: SNRequest, didFailWithError error: Error) {
        lock.lock();failed = true;lock.unlock();done.signal()
    }
    func requestDidComplete(_ request: SNRequest) { done.signal() }
    func snapshot() throws -> [[String: Any]] {
        lock.lock();defer { lock.unlock() }
        if failed || windows.isEmpty { throw ProtocolFault.unavailable }
        return windows
    }
}

do {
    let request = try SNClassifySoundRequest(classifierIdentifier: .version1)
    request.windowDuration = CMTime(seconds: 2, preferredTimescale: 16_000)
    request.overlapFactor = 0
    let known = Set(request.knownClassifications)
    guard known.count <= 1024, request.windowDuration.seconds >= 1.5,
          request.windowDuration.seconds <= 2.001,
          let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000,
                                     channels: 1, interleaved: false) else { throw ProtocolFault.unavailable }
    emit(["type": "ready", "version": 1, "sample_rate": 16_000,
          "known_classifications": known.sorted()])
    while let line = try packet() {
        try autoreleasepool {
            guard line.count <= 100_000,
                  let value = try JSONSerialization.jsonObject(with: line) as? [String: Any],
                  let id = value["id"] as? NSNumber, CFGetTypeID(id) != CFBooleanGetTypeID(),
                  id.doubleValue == Double(id.intValue), id.intValue > 0,
                  let encoded = value["pcm"] as? String, encoded.count <= 86_000,
                  let pcm = Data(base64Encoded: encoded), pcm.count == 64_000,
                  let requested = value["identifiers"] as? [String],
                  !requested.isEmpty, requested.count <= 16,
                  Set(requested).count == requested.count, Set(requested).isSubset(of: known),
                  let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 32_000),
                  let samples = buffer.int16ChannelData?[0] else { throw ProtocolFault.invalid }
            buffer.frameLength = 32_000
            pcm.withUnsafeBytes { raw in
                if let source = raw.baseAddress { memcpy(samples, source, pcm.count) }
            }
            let observer = Results(Set(requested))
            let analyzer = SNAudioStreamAnalyzer(format: format)
            defer { analyzer.removeAllRequests() }
            try analyzer.add(request, withObserver: observer)
            analyzer.analyze(buffer, atAudioFramePosition: 0)
            analyzer.completeAnalysis()
            guard observer.done.wait(timeout: .now()+4) == .success else { throw ProtocolFault.unavailable }
            emit(["type": "result", "version": 1, "id": id.intValue,
                  "windows": try observer.snapshot()])
        }
    }
} catch {
    // Never include the error description, PCM, path, or recognized content.
    emit(["type": "unavailable", "version": 1, "code": "sound_model_unavailable"])
    exit(69)
}
