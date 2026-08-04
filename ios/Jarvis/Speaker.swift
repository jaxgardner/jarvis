import AVFoundation
import Foundation

/// Where spoken audio comes from.
///
/// A stream of chunks rather than one `Data`, because that is what makes the
/// reply start quickly: the Mini sends the first clause as soon as it exists,
/// roughly 300ms in, instead of the whole reply about a second and a half in.
/// A protocol rather than a direct `JarvisAPI` reference so `SpeakerTests` can
/// drive both paths without a server or an audio device.
/// `@MainActor` because the real implementation reads the host and the device
/// token, which live there. The stream it returns is consumed off the actor,
/// which is the point — only building the request is isolated.
@MainActor
protocol SpeechSource {
    func audioChunks(for text: String) -> AsyncThrowingStream<Data, Error>
}

/// Speech out.
///
/// The server guarantees `reply` is a single plain-text string with no
/// markdown, lists, or emoji — written to be spoken. So this does no
/// processing at all; anything it "fixed up" would be papering over a server
/// bug that should be fixed at the source.
///
/// Two paths. The Mini synthesizes a neural voice and this plays the chunks as
/// they arrive; if that fails for any reason at all, `AVSpeechSynthesizer`
/// speaks the same string. The fallback is unconditional on purpose — a spoken
/// reply always happens, which is the same invariant `notify.push()` protects
/// by returning a bool instead of raising.
///
/// **The fallback is only available before the first sound.** Once a chunk has
/// been handed to the player, a failure mid-stream leaves the reply truncated
/// rather than repeating it in Apple's voice — hearing the first half of a
/// sentence twice is worse than hearing it once and stopping.
///
/// `AVAudioEngine` rather than `AVAudioPlayer`: the latter needs the whole
/// file up front, which is precisely the wait this design exists to remove.
@MainActor
final class Speaker: NSObject, ObservableObject {
    private let synthesizer = AVSpeechSynthesizer()
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var attached = false

    /// Buffers handed to the player that have not finished playing, and
    /// whether any more are coming. Together they say when the reply is over,
    /// which is when the audio session can be handed back to whatever was
    /// playing before.
    private var pending = 0
    private var streamEnded = false

    /// Which utterance is current. `speak` runs for as long as chunks keep
    /// arriving, so a new turn can begin while an old call is still inside its
    /// loop — and that call must not then schedule audio into the new turn or,
    /// worse, release the audio session the mic has just taken.
    private var generation = 0

    /// Set once by `TalkView`, which gets `JarvisAPI` from the environment and
    /// so cannot pass it to a `@StateObject`'s initializer.
    var source: SpeechSource?

    /// Whether the last utterance used Apple's voice. Tests read it; so could
    /// a debug screen, if "why does it sound wrong today" ever needs an answer
    /// on the device rather than in `/health`.
    private(set) var didFallBack = false

    init(source: SpeechSource? = nil) {
        self.source = source
        super.init()
        synthesizer.delegate = self
    }

    var isSpeaking: Bool { synthesizer.isSpeaking || player.isPlaying }

    func speak(_ text: String) async {
        guard !text.isEmpty else { return }
        stop()
        activateSession()
        let mine = generation

        guard let source else {
            fallBack(to: text)
            return
        }

        var started = false
        do {
            for try await chunk in source.audioChunks(for: text) {
                guard mine == generation else { return }
                guard let buffer = Self.buffer(fromWAV: chunk) else {
                    throw SpeechError.notAudio
                }
                try schedule(buffer)
                started = true
            }
            guard mine == generation else { return }
            if !started { throw SpeechError.silence }
            streamEnded = true
            if pending == 0 { finished() }
        } catch {
            guard mine == generation else { return }
            // Every failure before the first sound has the same remedy, so
            // they share a path: no source, transport, 503, a body that is not
            // a WAV. Distinguishing them would only be to log them.
            if started {
                streamEnded = true
                if pending == 0 { finished() }
            } else {
                fallBack(to: text)
            }
        }
    }

    func stop() {
        if synthesizer.isSpeaking {
            synthesizer.stopSpeaking(at: .immediate)
        }
        player.stop()
        if engine.isRunning { engine.stop() }
        pending = 0
        streamEnded = false
        // Retires any `speak` still iterating, and any buffer-completion
        // callback still in flight for the utterance being cut off.
        generation += 1
    }

    private enum SpeechError: Error {
        case notAudio
        case silence
    }

    // MARK: - Session

    /// The mic's session, reused rather than replaced.
    ///
    /// `Transcriber` configures `.playAndRecord` and leaves it active exactly
    /// so this can play through it. Setting `.playback` here instead would
    /// force a route change between capture ending and the reply starting —
    /// which is the thing `Transcriber.configureSession` is written to avoid,
    /// and it costs the first syllable.
    private func activateSession() {
        let session = AVAudioSession.sharedInstance()
        if session.category != .playAndRecord {
            try? session.setCategory(
                .playAndRecord,
                mode: .spokenAudio,
                options: [.duckOthers, .defaultToSpeaker, .allowBluetoothHFP]
            )
        }
        try? session.setActive(true)
    }

    /// The reply is over: stop ducking whatever else was playing.
    ///
    /// Deferred to here rather than done in `Transcriber.stop()` because the
    /// session has to stay up across the gap between capture ending and the
    /// audio arriving.
    private func finished() {
        if engine.isRunning { engine.stop() }
        try? AVAudioSession.sharedInstance().setActive(
            false, options: .notifyOthersOnDeactivation
        )
    }

    // MARK: - Playback

    private func schedule(_ buffer: AVAudioPCMBuffer) throws {
        if !attached {
            engine.attach(player)
            attached = true
        }
        if !engine.isRunning {
            engine.connect(player, to: engine.mainMixerNode, format: buffer.format)
            engine.prepare()
            try engine.start()
        }

        pending += 1
        player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) {
            [weak self] _ in
            Task { @MainActor in self?.bufferFinished() }
        }
        if !player.isPlaying { player.play() }
        didFallBack = false
    }

    private func bufferFinished() {
        pending = max(0, pending - 1)
        if pending == 0 && streamEnded { finished() }
    }

    /// A chunk of `audio/x-jarvis-chunked-wav` as something the engine can play.
    ///
    /// Returns nil for anything that is not a PCM16 WAV, which is what keeps a
    /// stray error body from being played as noise — the framing carries no
    /// checksum, so being unable to parse it *is* the validation.
    ///
    /// Converts to float rather than scheduling the 16-bit samples directly:
    /// the standard float format is what `AVAudioEngine` mixes in natively, so
    /// there is no format negotiation to get wrong.
    nonisolated static func buffer(fromWAV data: Data) -> AVAudioPCMBuffer? {
        guard let wave = WAV(data) else { return nil }
        guard
            let format = AVAudioFormat(
                standardFormatWithSampleRate: Double(wave.sampleRate),
                channels: AVAudioChannelCount(wave.channels)
            ),
            let buffer = AVAudioPCMBuffer(
                pcmFormat: format,
                frameCapacity: AVAudioFrameCount(wave.frameCount)
            )
        else { return nil }

        buffer.frameLength = AVAudioFrameCount(wave.frameCount)
        guard let channels = buffer.floatChannelData else { return nil }

        wave.samples.withUnsafeBufferPointer { source in
            for frame in 0..<wave.frameCount {
                for channel in 0..<wave.channels {
                    channels[channel][frame] =
                        Float(source[frame * wave.channels + channel]) / 32768.0
                }
            }
        }
        return buffer
    }

    // MARK: - Fallback

    private func fallBack(to text: String) {
        didFallBack = true
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = Self.preferredVoice()
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate
        synthesizer.speak(utterance)
    }

    /// Prefer a premium/enhanced voice when the user has downloaded one —
    /// the default compact voice is noticeably robotic for full sentences.
    private static func preferredVoice() -> AVSpeechSynthesisVoice? {
        let language = AVSpeechSynthesisVoice.currentLanguageCode()
        let candidates = AVSpeechSynthesisVoice.speechVoices()
            .filter { $0.language == language }
        return candidates.first { $0.quality == .premium }
            ?? candidates.first { $0.quality == .enhanced }
            ?? AVSpeechSynthesisVoice(language: language)
    }
}

extension Speaker: AVSpeechSynthesizerDelegate {
    nonisolated func speechSynthesizer(
        _ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance
    ) {
        Task { @MainActor [weak self] in self?.finished() }
    }

    nonisolated func speechSynthesizer(
        _ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance
    ) {
        Task { @MainActor [weak self] in self?.finished() }
    }
}

/// Just enough RIFF to read what the Mini sends.
///
/// Walks the chunk table rather than assuming a 44-byte header: canonical is
/// what `wave` writes today, but a parser that silently mis-reads a file with
/// one extra chunk would produce noise, and noise is the one output worse than
/// falling back to Apple.
private struct WAV {
    let sampleRate: Int
    let channels: Int
    let frameCount: Int
    let samples: [Int16]

    init?(_ data: Data) {
        guard data.count >= 12,
            data[0..<4].elementsEqual("RIFF".utf8),
            data[8..<12].elementsEqual("WAVE".utf8)
        else { return nil }

        var sampleRate = 0
        var channels = 0
        var bits = 0
        var body: Data?

        var offset = 12
        while offset + 8 <= data.count {
            let id = data[data.startIndex + offset..<data.startIndex + offset + 4]
            let size = Int(data.u32(at: offset + 4))
            let start = offset + 8
            guard size >= 0, start + size <= data.count else { break }

            if id.elementsEqual("fmt ".utf8), size >= 16 {
                channels = Int(data.u16(at: start + 2))
                sampleRate = Int(data.u32(at: start + 4))
                bits = Int(data.u16(at: start + 14))
            } else if id.elementsEqual("data".utf8) {
                body = data[data.startIndex + start..<data.startIndex + start + size]
            }
            // Chunks are word-aligned; an odd size is followed by a pad byte.
            offset = start + size + (size % 2)
        }

        guard bits == 16, channels > 0, sampleRate > 0, let body, !body.isEmpty
        else { return nil }

        var samples = [Int16](repeating: 0, count: body.count / 2)
        samples.withUnsafeMutableBytes { destination in
            _ = body.copyBytes(to: destination, count: (body.count / 2) * 2)
        }
        // WAV is little-endian; every device this runs on is too, but the
        // conversion is free and makes the assumption explicit.
        if Int16(littleEndian: 1) != 1 {
            samples = samples.map { Int16(littleEndian: $0) }
        }

        self.sampleRate = sampleRate
        self.channels = channels
        self.frameCount = samples.count / channels
        self.samples = samples
        if frameCount == 0 { return nil }
    }
}

extension Data {
    fileprivate func u16(at offset: Int) -> UInt16 {
        UInt16(self[startIndex + offset]) | UInt16(self[startIndex + offset + 1]) << 8
    }

    fileprivate func u32(at offset: Int) -> UInt32 {
        UInt32(self[startIndex + offset]) | UInt32(self[startIndex + offset + 1]) << 8
            | UInt32(self[startIndex + offset + 2]) << 16
            | UInt32(self[startIndex + offset + 3]) << 24
    }
}
