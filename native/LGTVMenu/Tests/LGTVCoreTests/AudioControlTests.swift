import CoreAudio
import Foundation
import LGTVDSP
import XCTest
@testable import LGTVCore

final class AudioControlTests: XCTestCase {
    func testCurrentAndLegacyTVAudioPayloads() throws {
        let current = try XCTUnwrap(TVAudioState.parse(["returnValue": true, "volumeStatus": ["volume": 17, "muteStatus": false, "maxVolume": 100, "adjustVolume": true]]))
        XCTAssertEqual(current, TVAudioState(volume: 17, muted: false))
        XCTAssertEqual(TVAudioState.parse(["volume": 8, "mute": true]), TVAudioState(volume: 8, muted: true))
        XCTAssertEqual(TVAudioState.parse(["mute": true], previous: current), TVAudioState(volume: 17, muted: true))
        XCTAssertNil(TVAudioState.parse(["volume": 8]))
        for value: Any in [-1, 101, true, 1.2, Double.nan, "12"] {
            XCTAssertNil(TVAudioState.parse(["volume": value, "mute": false]))
        }
        XCTAssertNil(TVAudioState.parse(["returnValue": false, "volume": 12, "mute": false]))
    }

    func testAudioPermissionIsExplicitAndDoesNotBroadenPowerOnlyPairing() throws {
        let power = WebOSProtocol.registration(clientKey: nil)["payload"] as! [String: Any]
        let audio = WebOSProtocol.registration(clientKey: nil, audioControl: true)["payload"] as! [String: Any]
        let a = (audio["manifest"] as! [String: Any])["permissions"] as! [String]
        let p = (power["manifest"] as! [String: Any])["permissions"] as! [String]
        XCTAssertEqual(Set(a).subtracting(p), ["CONTROL_AUDIO"])
        XCTAssertFalse(p.contains("CONTROL_AUDIO"))
        let state = TVAudioState(volume: 8, muted: false, maximum: 50)
        XCTAssertEqual(try state.requestedVolume(scalar: 0.5), 25)
        for scalar in [-0.1, 1.1, Double.nan, Double.infinity] { XCTAssertThrowsError(try state.requestedVolume(scalar: scalar)) }
        XCTAssertThrowsError(try TVAudioState(volume: 8, muted: false, adjustable: false).requestedVolume(scalar: 0.2))
    }

    func testLoopbackPreservesPCMAndRejectsMissingOrOverwrittenFrames() throws {
        let ring = try XCTUnwrap(LGTVRingCreate()); defer { LGTVRingDestroy(ring) }
        let source: [Float] = [0.75, -0.5, 0.125, -0.25, 0.9, 0.8]
        var result = [Float](repeating: 42, count: 6)
        LGTVRingRead(ring, -1, 3, &result); XCTAssertEqual(result, [0, 0, 0, 0, 0, 0])
        LGTVRingWrite(ring, 65535, 3, source)
        LGTVRingRead(ring, 65535, 3, &result); XCTAssertEqual(result, source)
        LGTVRingRead(ring, 0, 3, &result); XCTAssertEqual(result, [0, 0, 0, 0, 0, 0])
        LGTVRingWrite(ring, 131071, 3, source)
        LGTVRingRead(ring, 65535, 3, &result); XCTAssertEqual(result, [0, 0, 0, 0, 0, 0])
        LGTVRingReset(ring)
        LGTVRingRead(ring, 131071, 3, &result); XCTAssertEqual(result, [0, 0, 0, 0, 0, 0])
    }

    func testForwarderCopiesOnlyHDMIChannelsAndNeverFeedsVirtualOutput() {
        var input: [Float] = [0.75, -0.5, 0.25, -0.125]
        var output = [Float](repeating: 9, count: 8)
        input.withUnsafeMutableBytes { source in
            output.withUnsafeMutableBytes { destination in
                var i = AudioBufferList(mNumberBuffers: 1, mBuffers: AudioBuffer(mNumberChannels: 2, mDataByteSize: UInt32(source.count), mData: source.baseAddress))
                var o = AudioBufferList(mNumberBuffers: 1, mBuffers: AudioBuffer(mNumberChannels: 4, mDataByteSize: UInt32(destination.count), mData: destination.baseAddress))
                XCTAssertTrue(LGTVRouteAudio(&i, &o, 2))
            }
        }
        XCTAssertEqual(output, [0, 0, 0.75, -0.5, 0, 0, 0.25, -0.125])
    }
}
