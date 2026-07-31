import AVFoundation
import Foundation

/// Speech out.
///
/// The server guarantees `reply` is a single plain-text string with no
/// markdown, lists, or emoji — written to be spoken. So this does no
/// processing at all; anything it "fixed up" would be papering over a server
/// bug that should be fixed at the source.
@MainActor
final class Speaker: ObservableObject {
    private let synthesizer = AVSpeechSynthesizer()

    var isSpeaking: Bool { synthesizer.isSpeaking }

    func speak(_ text: String) {
        guard !text.isEmpty else { return }
        stop()

        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(
            .playback, mode: .spokenAudio, options: [.duckOthers]
        )
        try? session.setActive(true)

        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = Self.preferredVoice()
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate
        synthesizer.speak(utterance)
    }

    func stop() {
        if synthesizer.isSpeaking {
            synthesizer.stopSpeaking(at: .immediate)
        }
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
