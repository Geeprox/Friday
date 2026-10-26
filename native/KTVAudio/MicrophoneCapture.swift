import AVFoundation
import CoreAudio
import Foundation

// Capture has its own hardware clock; a silent render graph cannot switch the mic off.
final class MicrophoneCapture {
    let input: Device
    private var procedure: AudioDeviceIOProcID?
    private let ring: MusicRing
    private let source: AVAudioSourceNode
    private(set) var active = false

    init(input: Device, engine: AVAudioEngine) throws {
        self.input = input
        guard input.inputs == 2, input.outputs == 0,
              let format = scalar(input.id, kAudioDevicePropertyStreamFormat,
                                  AudioStreamBasicDescription(), kAudioObjectPropertyScopeInput),
              format.mFormatID == kAudioFormatLinearPCM, format.mBitsPerChannel == 32,
              format.mFormatFlags & kAudioFormatFlagIsFloat != 0,
              format.mChannelsPerFrame == 2, format.mSampleRate > 0,
              let renderingFormat = AVAudioFormat(standardFormatWithSampleRate: format.mSampleRate, channels: 2) else {
            throw ProbeFailure(code: "microphone_format_unavailable")
        }
        let queue = MusicRing(format); ring = queue
        source = AVAudioSourceNode(format: renderingFormat) { _, _, frames, output in
            queue.render(Int(frames), output); return noErr
        }
        engine.attach(source)
        engine.connect(source, to: engine.mainMixerNode, fromBus: 0, toBus: 0, format: renderingFormat)
        guard AudioDeviceCreateIOProcIDWithBlock(&procedure, input.id, nil, { _, data, _, _, _ in queue.push(data) }) == noErr else {
            throw ProbeFailure(code: "microphone_capture_unavailable")
        }
    }

    var sourcesAvailable: Bool {
        label(input.id, kAudioDevicePropertyDeviceUID) == input.uid &&
        scalar(input.id, kAudioDevicePropertyDeviceIsAlive, UInt32(0)) == 1
    }

    func start() throws {
        guard sourcesAvailable, let procedure, AudioDeviceStart(input.id, procedure) == noErr else {
            throw ProbeFailure(code: "microphone_capture_unavailable")
        }
        active = true
    }
    var report: [String: Any] { ring.report }
    func stop() {
        if let procedure {
            if active { AudioDeviceStop(input.id, procedure) }
            AudioDeviceDestroyIOProcID(input.id, procedure)
        }
        procedure = nil; active = false
    }
    deinit { stop() }
}
