import Accelerate
import AVFoundation
import Foundation
import Speech
import Synchronization

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

    /// Set when the endpointer decides you have stopped talking. The owner
    /// watches it and responds by calling `stop()`.
    ///
    /// A published latch rather than a callback: a closure owned by the
    /// transcriber that captures the view that owns the transcriber is a
    /// retain cycle, and `LaunchRouter` already establishes this pattern for
    /// the other direction of the same conversation.
    @Published private(set) var didEndpoint = false

    private let engine = AVAudioEngine()
    private var analyzer: SpeechAnalyzer?
    private var module: SpeechTranscriber?
    private var continuation: AsyncStream<AnalyzerInput>.Continuation?
    private var resultsTask: Task<Void, Never>?
    private var endpointer: Endpointer?

    /// Finalized text accumulates; volatile text is the model's current guess
    /// at the tail and is replaced wholesale each time it changes.
    private var finalized = ""
    private var volatileTail = ""

    // MARK: - Lifecycle

    /// - Parameter pauseToSend: how long a silence means "done talking". Pass
    ///   `nil` to disable endpointing entirely, which leaves the mic running
    ///   until the button is tapped.
    func start(pauseToSend: TimeInterval?) async throws {
        guard !isListening else { return }
        finalized = ""
        volatileTail = ""
        transcript = ""
        didEndpoint = false

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

        endpointer = pauseToSend.map { pause in
            // The callback runs on the realtime audio thread, so it does the
            // one thing that is safe there: hop to the main actor.
            Endpointer(pause: pause) { [weak self] in
                Task { @MainActor in self?.endpointReached() }
            }
        }

        try await analyzer.start(inputSequence: stream)
        try startEngine(target: analyzerFormat, continuation: continuation)
        isListening = true
    }

    /// The endpointer heard a pause. Only act on it if there is something to
    /// send — a door slam has the energy profile of speech but produces no
    /// text, and stopping on one would silently kill the session.
    private func endpointReached() {
        guard isListening else { return }
        guard !transcript.isEmpty else {
            endpointer?.rearm()
            return
        }
        didEndpoint = true
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
        endpointer = nil

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
        let endpointer = self.endpointer

        // This closure runs on a realtime audio thread. It touches nothing on
        // the main actor — only the continuation, its own converter, and the
        // endpointer, which is measured against the *input* buffer: it is
        // always float32 from the input node, whereas the analyzer's preferred
        // format need not be.
        input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { buffer, _ in
            endpointer?.ingest(buffer)
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

/// Decides when you have stopped talking.
///
/// Energy, not text. The transcriber's results are the obvious signal and the
/// wrong one: it emits when it has something to say, so "no new text" means
/// either a pause or a model still chewing, and those need opposite responses.
/// Frame energy answers the actual question directly and about a second sooner.
///
/// Lives on the realtime audio thread. No allocation, no locks, no clock calls
/// — elapsed time comes from counting samples, which is also exact.
///
/// Internal rather than private so `EndpointerTests` can drive it with
/// synthetic buffers. Counting samples instead of reading a clock is what makes
/// that possible: five seconds of audio is five seconds whether it arrives in
/// realtime or in a loop.
final class Endpointer {
    /// Silence this long ends the utterance.
    private let pause: TimeInterval
    /// Speech must be heard for this long before a silence counts. Otherwise
    /// the quiet moment between opening the app and starting to speak is
    /// indistinguishable from the one after finishing.
    private let minimumSpeech: TimeInterval = 0.35
    /// Below this RMS nothing is speech, however quiet the room is. Roughly
    /// -44 dBFS; conversational speech at arm's length runs 20–30 dB above it.
    private let absoluteFloor: Float = 0.006
    /// How far above the room's own noise a frame must sit to count as voice.
    private let voiceRatio: Float = 5
    /// …but never demand more than this, whatever the measured floor says.
    ///
    /// Without the cap the adaptive threshold has a trap: open the mic while
    /// someone is already talking — which is exactly what the Action Button
    /// does — and the floor calibrates to speech, so the threshold lands above
    /// speech and the endpointer goes deaf for the whole utterance. A ceiling
    /// costs nothing in a quiet room, where the absolute floor is what binds,
    /// and it is what makes a café work: 0.03 is unambiguously a voice at
    /// arm's length and comfortably above room noise you can still be heard over.
    private let adaptiveCeiling: Float = 0.03

    private let onFire: () -> Void

    /// The one piece of state another thread writes. Everything else below is
    /// touched only by the audio thread, so an atomic handoff is all the
    /// synchronization needed — and the only kind allowed here, since a lock
    /// on the audio thread is how you get dropouts.
    private let rearmRequested = Atomic<Bool>(false)

    private var noiseFloor: Float?
    private var voicedSeconds: TimeInterval = 0
    private var silentSeconds: TimeInterval = 0
    private var fired = false

    init(pause: TimeInterval, onFire: @escaping () -> Void) {
        self.pause = pause
        self.onFire = onFire
    }

    func ingest(_ buffer: AVAudioPCMBuffer) {
        if rearmRequested.exchange(false, ordering: .relaxed) {
            voicedSeconds = 0
            silentSeconds = 0
            fired = false
        }
        guard !fired,
              let samples = buffer.floatChannelData?[0],
              buffer.frameLength > 0
        else { return }

        var rms: Float = 0
        vDSP_rmsqv(samples, 1, &rms, vDSP_Length(buffer.frameLength))
        let seconds = Double(buffer.frameLength) / buffer.format.sampleRate

        // Track the floor down instantly and up very slowly. A running minimum
        // settles on true room tone within the first pause; an average would
        // be dragged upward by the speech it is meant to be measured against.
        let floor = noiseFloor ?? rms
        noiseFloor = rms < floor ? rms : floor + (rms - floor) * 0.001

        let threshold = min(adaptiveCeiling, max(absoluteFloor, floor * voiceRatio))
        if rms > threshold {
            voicedSeconds += seconds
            silentSeconds = 0
        } else if voicedSeconds >= minimumSpeech {
            silentSeconds += seconds
            if silentSeconds >= pause {
                fired = true
                onFire()
            }
        }
    }

    /// Keep listening after a false alarm — energy that produced no text.
    /// The measured noise floor is worth keeping; the speech tally is not.
    func rearm() {
        rearmRequested.store(true, ordering: .relaxed)
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
