import AVFoundation
import Foundation

/// Where spoken audio comes from.
///
/// A protocol rather than a direct `JarvisAPI` reference so `SpeakerTests` can
/// drive both paths without a server or an audio device.
protocol SpeechSource {
    func audio(for text: String) async throws -> Data
}

/// Speech out.
///
/// The server guarantees `reply` is a single plain-text string with no
/// markdown, lists, or emoji — written to be spoken. So this does no
/// processing at all; anything it "fixed up" would be papering over a server
/// bug that should be fixed at the source.
///
/// Two paths. The Mini synthesizes a British neural voice and this plays the
/// WAV; if that fails for any reason at all, `AVSpeechSynthesizer` speaks the
/// same string. The fallback is unconditional on purpose — a spoken reply
/// always happens, which is the same invariant `notify.push()` protects by
/// returning a bool instead of raising.
@MainActor
final class Speaker: ObservableObject {
    private let synthesizer = AVSpeechSynthesizer()
    private var player: AVAudioPlayer?

    /// Set once by `TalkView`, which gets `JarvisAPI` from the environment and
    /// so cannot pass it to a `@StateObject`'s initializer.
    var source: SpeechSource?

    /// Whether the last utterance used Apple's voice. Tests read it; so could
    /// a debug screen, if "why does it sound wrong today" ever needs an answer
    /// on the device rather than in `/health`.
    private(set) var didFallBack = false

    init(source: SpeechSource? = nil) {
        self.source = source
    }

    var isSpeaking: Bool { synthesizer.isSpeaking || (player?.isPlaying ?? false) }

    func speak(_ text: String) async {
        guard !text.isEmpty else { return }
        stop()
        activateSession()

        // `try?` swallows the error deliberately: every failure mode here —
        // no source, transport, 503, a body that is not a WAV — has the same
        // remedy, and distinguishing them would only be to log them.
        if let source, let data = try? await source.audio(for: text), play(data) {
            didFallBack = false
            return
        }

        didFallBack = true
        speakLocally(text)
    }

    func stop() {
        if synthesizer.isSpeaking {
            synthesizer.stopSpeaking(at: .immediate)
        }
        player?.stop()
        player = nil
    }

    private func activateSession() {
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(
            .playback, mode: .spokenAudio, options: [.duckOthers]
        )
        try? session.setActive(true)
    }

    private func play(_ data: Data) -> Bool {
        guard let player = try? AVAudioPlayer(data: data) else { return false }
        self.player = player
        return player.play()
    }

    private func speakLocally(_ text: String) {
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
