import AVFoundation
import Foundation
import Speech

enum TranscriberError: LocalizedError {
    case microphoneDenied
    case noModel

    var errorDescription: String? {
        switch self {
        case .microphoneDenied:
            return "Microphone access is off. Turn it on in Settings."
        case .noModel:
            return "The on-device speech model isn't available for this language."
        }
    }
}

/// Speech in, using iOS 26's `SpeechAnalyzer` / `SpeechTranscriber`.
///
/// On device, always — nothing said to Jarvis leaves the phone before the
/// text reaches the Mini, and the whole point of the fast path is that it is
/// fast. The older `SFSpeechRecognizer` remains the documented fallback if a
/// locale has no `SpeechTranscriber` model.
@MainActor
final class Transcriber: ObservableObject {
    @Published private(set) var transcript = ""
    @Published private(set) var isListening = false
    @Published private(set) var status = ""

    private let engine = AVAudioEngine()
    private var analyzer: SpeechAnalyzer?
    private var module: SpeechTranscriber?
    private var continuation: AsyncStream<AnalyzerInput>.Continuation?
    private var resultsTask: Task<Void, Never>?

    /// Finalized text accumulates; volatile text is the model's current guess
    /// at the tail and is replaced wholesale each time it changes.
    private var finalized = ""
    private var volatileTail = ""

    // MARK: - Lifecycle

    func start() async throws {
        guard !isListening else { return }
        finalized = ""
        volatileTail = ""
        transcript = ""

        guard await Self.requestMicrophone() else { throw TranscriberError.microphoneDenied }

        let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale.current)
            ?? Locale(identifier: "en-US")
        let module = SpeechTranscriber(locale: locale, preset: .progressiveTranscription)
        self.module = module

        try await installModelIfNeeded(for: module, locale: locale)

        let analyzer = SpeechAnalyzer(modules: [module])
        self.analyzer = analyzer
        let analyzerFormat = await SpeechAnalyzer.bestAvailableAudioFormat(
            compatibleWith: [module]
        )

        let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
        self.continuation = continuation

        resultsTask = Task { [weak self] in
            do {
                for try await result in module.results {
                    let text = String(result.text.characters)
                    let isFinal = result.isFinal
                    await MainActor.run { self?.apply(text, isFinal: isFinal) }
                }
            } catch {
                await MainActor.run { self?.status = error.localizedDescription }
            }
        }

        try await analyzer.start(inputSequence: stream)
        try startEngine(target: analyzerFormat, continuation: continuation)
        isListening = true
    }

    /// Stops capture and returns the complete transcript.
    ///
    /// Deliberately awaits the results task rather than cancelling it: the
    /// last words spoken are still in flight when the tap comes off, and
    /// dropping them means the reminder loses its time ("remind me to call
    /// the dentist at" — and nothing).
    @discardableResult
    func stop() async -> String {
        guard isListening else { return transcript }
        isListening = false

        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        continuation?.finish()
        continuation = nil

        try? await analyzer?.finalizeAndFinishThroughEndOfInput()
        await resultsTask?.value
        resultsTask = nil
        analyzer = nil
        module = nil

        try? AVAudioSession.sharedInstance().setActive(
            false, options: .notifyOthersOnDeactivation
        )
        return transcript
    }

    // MARK: - Audio

    private func startEngine(
        target: AVAudioFormat?,
        continuation: AsyncStream<AnalyzerInput>.Continuation
    ) throws {
        let session = AVAudioSession.sharedInstance()
        // .playAndRecord, not .record: the reply is spoken the moment capture
        // ends, and swapping categories in between clips the first syllable.
        try session.setCategory(
            .playAndRecord,
            mode: .spokenAudio,
            options: [.duckOthers, .defaultToSpeaker, .allowBluetoothHFP]
        )
        try session.setActive(true)

        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)
        let resampler = Resampler()

        // This closure runs on a realtime audio thread. It touches nothing on
        // the main actor — only the continuation and its own converter.
        input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { buffer, _ in
            guard let target else {
                continuation.yield(AnalyzerInput(buffer: buffer))
                return
            }
            if let converted = resampler.convert(buffer, to: target) {
                continuation.yield(AnalyzerInput(buffer: converted))
            }
        }

        engine.prepare()
        try engine.start()
    }

    private func apply(_ text: String, isFinal: Bool) {
        if isFinal {
            finalized += text
            volatileTail = ""
        } else {
            volatileTail = text
        }
        transcript = (finalized + volatileTail)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: - Model assets

    private func installModelIfNeeded(for module: SpeechTranscriber, locale: Locale) async throws {
        if await AssetInventory.status(forModules: [module]) == .installed { return }

        status = "Downloading the speech model…"
        defer { status = "" }

        guard let request = try await AssetInventory.assetInstallationRequest(
            supporting: [module]
        ) else {
            // Nothing to install and not installed: this locale has no model.
            if await AssetInventory.status(forModules: [module]) != .installed {
                throw TranscriberError.noModel
            }
            return
        }
        try await request.downloadAndInstall()
        // Reserving keeps the model from being evicted between launches.
        _ = try? await AssetInventory.reserve(locale: locale)
    }

    private static func requestMicrophone() async -> Bool {
        if AVAudioApplication.shared.recordPermission == .granted { return true }
        return await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission { granted in
                continuation.resume(returning: granted)
            }
        }
    }
}

/// Bridges the microphone's native format to whatever the analyzer wants.
///
/// The input node hands out 48 kHz float; the analyzer asks for its own
/// preferred format and silently produces nothing useful if fed the wrong
/// one. Held as a class so the converter survives across tap callbacks —
/// rebuilding it per buffer is both slow and lossy.
private final class Resampler {
    private var converter: AVAudioConverter?
    private var sourceFormat: AVAudioFormat?

    func convert(_ buffer: AVAudioPCMBuffer, to target: AVAudioFormat) -> AVAudioPCMBuffer? {
        if buffer.format == target { return buffer }

        if converter == nil || sourceFormat != buffer.format {
            converter = AVAudioConverter(from: buffer.format, to: target)
            converter?.primeMethod = .none
            sourceFormat = buffer.format
        }
        guard let converter else { return nil }

        let ratio = target.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1024
        guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else {
            return nil
        }

        var supplied = false
        var error: NSError?
        converter.convert(to: output, error: &error) { _, status in
            if supplied {
                status.pointee = .noDataNow
                return nil
            }
            supplied = true
            status.pointee = .haveData
            return buffer
        }
        return error == nil && output.frameLength > 0 ? output : nil
    }
}
