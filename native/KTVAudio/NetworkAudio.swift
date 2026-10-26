import AVFoundation
import Darwin
import Foundation

// The child owns only the explicitly selected JBL connection. PCM lives in an anonymous pipe.
final class NetworkAudio {
    private let process = Process(), input = Pipe(), lock = NSLock()
    private let reportURL: URL
    private var converter: AVAudioConverter?
    private var closed = false, failed = false
    private var droppedFrames: UInt64 = 0
    var childProcess: Process { process }
    private let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 44100,
                                       channels: 2, interleaved: true)!

    init(root: URL, endpoint: [String: String]) throws {
        guard let identifier = endpoint["identifier"], let host = endpoint["host"] else {
            throw ProbeFailure(code: "network_target_unavailable")
        }
        reportURL = root.appendingPathComponent(".artifacts/ktv-audio/network-listen-\(UUID().uuidString).json")
        process.executableURL = root.appendingPathComponent("data/embodied/runtime/bin/python")
        process.arguments = [root.appendingPathComponent("scripts/ktv_network_audio.py").path,
                             "--stream", "--identifier", identifier, "--host", host,
                             "--report", reportURL.path, "--seconds", "30"]
        process.currentDirectoryURL = root
        process.standardInput = input
        process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
        signal(SIGPIPE, SIG_IGN)
        try process.run()
        try? input.fileHandleForReading.close()
        let fd = input.fileHandleForWriting.fileDescriptor
        let flags = fcntl(fd, F_GETFL)
        guard flags >= 0, fcntl(fd, F_SETFL, flags | O_NONBLOCK) >= 0 else {
            stop(); throw ProbeFailure(code: "network_pipe_unavailable")
        }
        var header = Data()
        func word<T: FixedWidthInteger>(_ value: T) {
            var little = value.littleEndian
            withUnsafeBytes(of: &little) { header.append(contentsOf: $0) }
        }
        let bytes = UInt32(44100 * 2 * 2 * 30)
        header.append(Data("RIFF".utf8)); word(bytes + 36); header.append(Data("WAVEfmt ".utf8))
        word(UInt32(16)); word(UInt16(1)); word(UInt16(2)); word(UInt32(44100)); word(UInt32(176400))
        word(UInt16(4)); word(UInt16(16)); header.append(Data("data".utf8)); word(bytes)
        let written = header.withUnsafeBytes { Darwin.write(fd, $0.baseAddress!, $0.count) }
        guard written == header.count else { stop(); throw ProbeFailure(code: "network_pipe_unavailable") }
    }

    func prepare(_ format: AVAudioFormat) throws {
        guard let conversion = AVAudioConverter(from: format, to: target) else {
            throw ProbeFailure(code: "network_audio_format_unavailable")
        }
        converter = conversion
    }

    func append(_ buffer: AVAudioPCMBuffer) {
        guard let converter else { return }
        let capacity = AVAudioFrameCount(ceil(Double(buffer.frameLength) * 44100 / buffer.format.sampleRate) + 32)
        guard let converted = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
        var used = false, error: NSError?
        converter.convert(to: converted, error: &error) { _, status in
            if used { status.pointee = .noDataNow; return nil }
            used = true; status.pointee = .haveData; return buffer
        }
        guard error == nil, converted.frameLength > 0,
              let source = converted.int16ChannelData?[0] else { return }
        let raw = UnsafeRawPointer(source), bytes = Int(converted.frameLength) * 4
        lock.lock(); defer { lock.unlock() }
        guard !closed, !failed else { return }
        let fd = input.fileHandleForWriting.fileDescriptor
        var offset = 0
        // <= PIPE_BUF: no partial PCM frames can be introduced by a full pipe.
        while offset < bytes {
            let size = min(512, bytes - offset)
            let written = Darwin.write(fd, raw + offset, size)
            if written == size { offset += size }
            else if written == -1 && (errno == EAGAIN || errno == EWOULDBLOCK) {
                droppedFrames += UInt64((bytes - offset) / 4); break
            } else { failed = true; break }
        }
    }

    var report: [String: Any] {
        lock.lock()
        let dropped = droppedFrames, pipeFailed = failed
        lock.unlock()
        var result: [String: Any] = ["status": process.isRunning ? "connecting" : "unavailable",
                                     "sender_dropped_frames": dropped, "pipe_failed": pipeFailed]
        if let data = try? Data(contentsOf: reportURL), data.count <= 65536,
           let child = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            for key in ["status", "code", "protocol_stream_started", "physical_audio_confirmed"] {
                if let value = child[key] { result[key] = value }
            }
        }
        if !process.isRunning, result["status"] as? String != "completed" {
            result["status"] = "unavailable"
            if result["code"] == nil { result["code"] = "network_worker_stopped" }
        }
        return result
    }

    func stop() {
        lock.lock()
        if closed { lock.unlock(); return }
        closed = true
        try? input.fileHandleForWriting.close()
        lock.unlock()
        guard process.isRunning else { return }
        process.terminate()
        let child = process
        DispatchQueue.global().asyncAfter(deadline: .now() + 3) {
            if child.isRunning { kill(child.processIdentifier, SIGKILL) }
        }
    }
    deinit { stop() }
}
