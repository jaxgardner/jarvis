import Foundation
import Testing

@testable import Jarvis

/// The fallback, which is the whole point of the design.
///
/// A spoken reply always happens. The Mini's voice is better, but it is on
/// the other side of a network from a device that is frequently on cellular,
/// and silence while a dead server is waited on is worse than the compact
/// voice — the compact voice is at least an assistant.
///
/// `AVAudioPlayer` itself is not under test. What is under test is which of
/// the two paths gets taken.
///
/// The 3-second timeout is not covered here and cannot honestly be: it lives
/// in `URLSession`, and a timeout surfaces as exactly the thrown error
/// `Failing` already models. Task 6 Step 4 exercises it on a real device,
/// which is the only place the number means anything.
struct SpeakerTests {
    struct Failing: SpeechSource {
        struct Unreachable: Error {}
        func audio(for text: String) async throws -> Data { throw Unreachable() }
    }

    struct Garbage: SpeechSource {
        func audio(for text: String) async throws -> Data { Data("503 not a wav".utf8) }
    }

    struct Working: SpeechSource {
        func audio(for text: String) async throws -> Data { SpeakerTests.silence() }
    }

    /// A quarter-second of silence as a real 24 kHz mono WAV. Hand-built
    /// rather than checked in as a fixture: forty-four bytes of header is
    /// less to explain than a binary blob in the repo.
    static func silence(frames: Int = 6_000, sampleRate: Int = 24_000) -> Data {
        var data = Data()
        func ascii(_ text: String) { data.append(contentsOf: Array(text.utf8)) }
        func u32(_ value: Int) {
            withUnsafeBytes(of: UInt32(value).littleEndian) { data.append(contentsOf: $0) }
        }
        func u16(_ value: Int) {
            withUnsafeBytes(of: UInt16(value).littleEndian) { data.append(contentsOf: $0) }
        }

        let payload = frames * 2
        ascii("RIFF"); u32(36 + payload); ascii("WAVE")
        ascii("fmt "); u32(16); u16(1); u16(1)
        u32(sampleRate); u32(sampleRate * 2); u16(2); u16(16)
        ascii("data"); u32(payload)
        data.append(Data(count: payload))
        return data
    }

    @MainActor @Test func anUnreachableServerFallsBackToApple() async {
        let speaker = Speaker(source: Failing())

        await speaker.speak("Reminder set for five o'clock.")

        #expect(speaker.didFallBack)
    }

    @MainActor @Test func undecodableAudioFallsBackToApple() async {
        let speaker = Speaker(source: Garbage())

        await speaker.speak("Reminder set for five o'clock.")

        #expect(speaker.didFallBack)
    }

    @MainActor @Test func noConfiguredSourceFallsBackToApple() async {
        let speaker = Speaker()

        await speaker.speak("Reminder set for five o'clock.")

        #expect(speaker.didFallBack)
    }

    @MainActor @Test func serverAudioIsPlayedInsteadOfApple() async {
        let speaker = Speaker(source: Working())

        await speaker.speak("Reminder set for five o'clock.")

        #expect(!speaker.didFallBack)
    }

    @MainActor @Test func emptyTextSaysNothingAtAll() async {
        let speaker = Speaker(source: Working())

        await speaker.speak("")

        #expect(!speaker.isSpeaking)
    }
}
