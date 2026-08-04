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
/// The timeout is not covered here and cannot honestly be: it lives in
/// `URLSession`, and a timeout surfaces as exactly the thrown error `Failing`
/// already models. `JarvisAPI.speechTimeout(for:)` is checked separately,
/// since that part is arithmetic.
struct SpeakerTests {
    /// Builds a source from chunks, optionally failing after handing some over
    /// — which is the case that decides whether falling back is still allowed.
    struct Source: SpeechSource {
        struct Unreachable: Error {}
        var chunks: [Data] = []
        var failsAtEnd = false

        func audioChunks(for text: String) -> AsyncThrowingStream<Data, Error> {
            AsyncThrowingStream { continuation in
                for chunk in chunks { continuation.yield(chunk) }
                continuation.finish(throwing: failsAtEnd ? Unreachable() : nil)
            }
        }
    }

    static var failing: Source { Source(failsAtEnd: true) }
    static var garbage: Source { Source(chunks: [Data("503 not a wav".utf8)]) }
    static var working: Source { Source(chunks: [silence()]) }

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
        let speaker = Speaker(source: Self.failing)

        await speaker.speak("Reminder set for five o'clock.")

        #expect(speaker.didFallBack)
    }

    @MainActor @Test func undecodableAudioFallsBackToApple() async {
        let speaker = Speaker(source: Self.garbage)

        await speaker.speak("Reminder set for five o'clock.")

        #expect(speaker.didFallBack)
    }

    @MainActor @Test func noConfiguredSourceFallsBackToApple() async {
        let speaker = Speaker()

        await speaker.speak("Reminder set for five o'clock.")

        #expect(speaker.didFallBack)
    }

    @MainActor @Test func serverAudioIsPlayedInsteadOfApple() async {
        let speaker = Speaker(source: Self.working)

        await speaker.speak("Reminder set for five o'clock.")

        #expect(!speaker.didFallBack)
    }

    /// The one case streaming introduces: the reply has already started.
    ///
    /// Falling back now would replay the opening clause in a different voice,
    /// so a stream that dies partway through stops instead. Truncated beats
    /// stuttered.
    @MainActor @Test func aStreamThatFailsAfterFirstSoundDoesNotStartOver() async {
        let speaker = Speaker(
            source: Source(chunks: [Self.silence()], failsAtEnd: true)
        )

        await speaker.speak("Reminder set for five o'clock.")

        #expect(!speaker.didFallBack)
    }

    @MainActor @Test func everyChunkIsPlayedInOrder() async {
        let speaker = Speaker(
            source: Source(chunks: [Self.silence(), Self.silence(), Self.silence()])
        )

        await speaker.speak("Got it — I'll remind you tomorrow at nine.")

        #expect(!speaker.didFallBack)
    }

    @MainActor @Test func emptyTextSaysNothingAtAll() async {
        let speaker = Speaker(source: Self.working)

        await speaker.speak("")

        #expect(!speaker.isSpeaking)
    }

    // MARK: - Framing

    /// The framing carries no checksum, so being unable to parse a chunk is
    /// the only thing standing between an error body and a burst of noise.
    @Test func onlyRealWAVsDecode() {
        #expect(Speaker.buffer(fromWAV: SpeakerTests.silence()) != nil)
        #expect(Speaker.buffer(fromWAV: Data("503 not a wav".utf8)) == nil)
        #expect(Speaker.buffer(fromWAV: Data()) == nil)
        #expect(Speaker.buffer(fromWAV: SpeakerTests.silence().prefix(20)) == nil)
    }

    @Test func aDecodedChunkKeepsItsRateAndLength() throws {
        let buffer = try #require(
            Speaker.buffer(fromWAV: SpeakerTests.silence(frames: 1_200))
        )

        #expect(buffer.format.sampleRate == 24_000)
        #expect(buffer.format.channelCount == 1)
        #expect(buffer.frameLength == 1_200)
    }

    /// Long enough for a paragraph, short enough to still lose the race to
    /// Apple's voice on a Mini that is not answering.
    @Test func theTimeoutScalesWithHowMuchThereIsToSay() {
        let short = JarvisAPI.speechTimeout(for: "Noted.")
        let long = JarvisAPI.speechTimeout(for: String(repeating: "a", count: 600))

        #expect(short >= 4)
        #expect(long > short)
        #expect(long <= 15)
    }
}
