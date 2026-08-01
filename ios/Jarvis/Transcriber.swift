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
///
/// **The mic opens before the analyzer does.** Building the speech stack costs
/// the better part of a second — several round trips to the speech daemon, then
/// `SpeechAnalyzer.start` — and doing it first put that whole second between
/// pressing the Action Button and being able to talk. So `start` does the
/// cheap half (session, engine, tap) and reports `isListening`, while the
/// expensive half runs behind it and captured audio waits in an unbounded
/// stream. Words spoken during setup are transcribed late, not lost.
@MainActor
final class Transcriber: ObservableObject {
    @Published private(set) var transcript = ""
    @Published private(set) var isListening = false
    @Published private(set) var status = ""

    /// Set when the speech stack fails to come up behind an already-open mic.
    /// It cannot be thrown from `start`, which has long since returned.
    @Published private(set) var failure: String?

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
    /// Raw audio out of the tap. Finished by `stop`, which is what eventually
    /// drains `feed` and closes the analyzer's own input.
    private var capture: AsyncStream<CapturedAudio>.Continuation?
    private var sessionTask: Task<Void, Never>?
    private var resultsTask: Task<Void, Never>?
    private var endpointer: Endpointer?

    /// Finalized text accumulates; volatile text is the model's current guess
    /// at the tail and is replaced wholesale each time it changes.
    private var finalized = ""
    private var volatileTail = ""

    // MARK: - Lifecycle

    /// Pay the speech stack's fixed costs now so the next press doesn't.
    ///
    /// Cheap to call, idempotent, and worth calling from anywhere that knows
    /// the mic is likely next — the Talk screen appearing is the obvious one.
    /// It cannot help a cold Action-Button launch, where this and `start`
    /// begin in the same instant; that case is what opening the mic first is
    /// for.
    func prepare() {
        try? Self.configureSession()
        Task { _ = try? await Self.warmed(reporting: self) }
    }

    /// - Parameter pauseToSend: how long a silence means "done talking". Pass
    ///   `nil` to disable endpointing entirely, which leaves the mic running
    ///   until the button is tapped.
    func start(pauseToSend: TimeInterval?) async throws {
        guard !isListening else { return }
        finalized = ""
        volatileTail = ""
        transcript = ""
        didEndpoint = false
        failure = nil

        guard await Self.requestMicrophone() else { throw TranscriberError.microphoneDenied }

        // The one case that still waits: a launch that has never reached a
        // running analyzer may have a speech model to download, and holding
        // the mic open through a download would be both pointless and rude.
        if !Self.hasTranscribedBefore {
            _ = try await Self.warmed(reporting: self)
        }

        endpointer = pauseToSend.map { pause in
            // The callback runs on the realtime audio thread, so it does the
            // one thing that is safe there: hop to the main actor.
            Endpointer(pause: pause) { [weak self] in
                Task { @MainActor in self?.endpointReached() }
            }
        }

        let (captured, capture) = AsyncStream<CapturedAudio>.makeStream()
        self.capture = capture
        do {
            try startEngine(into: capture)
        } catch {
            self.capture = nil
            endpointer = nil
            throw error
        }
        isListening = true

        sessionTask = Task { [weak self] in await self?.transcribe(captured) }
    }

    /// Everything that was in `start` before the mic became the first thing to
    /// open. Runs with audio already accumulating in `captured`.
    private func transcribe(_ captured: AsyncStream<CapturedAudio>) async {
        do {
            let warm = try await Self.warmed(reporting: self)
            let module = SpeechTranscriber(locale: warm.locale, preset: .progressiveTranscription)
            self.module = module

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

            let analyzer = SpeechAnalyzer(modules: [module])
            self.analyzer = analyzer

            let (inputs, input) = AsyncStream<AnalyzerInput>.makeStream()
            try await analyzer.start(inputSequence: inputs)
            Self.hasTranscribedBefore = true

            // Returns when `stop` finishes the capture stream, having handed
            // over every buffer that was taken before it did.
            await Self.feed(captured, into: input, resampling: warm.format)
        } catch {
            failure = error.localizedDescription
            abort()
        }
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
    /// Deliberately awaits the session and results tasks rather than cancelling
    /// them: the last words spoken are still in flight when the tap comes off,
    /// and dropping them means the reminder loses its time ("remind me to call
    /// the dentist at" — and nothing). Awaiting the session task also covers
    /// the short-utterance case the split introduced, where the analyzer is
    /// still coming up at the moment you stop talking.
    @discardableResult
    func stop() async -> String {
        guard isListening else { return transcript }
        isListening = false

        closeMic()

        await sessionTask?.value
        sessionTask = nil

        try? await analyzer?.finalizeAndFinishThroughEndOfInput()
        await resultsTask?.value
        resultsTask = nil
        analyzer = nil
        module = nil

        deactivateSession()
        return transcript
    }

    /// The speech stack failed to come up with the mic already open. Nothing
    /// can transcribe what is being captured, so close it rather than record
    /// into a void. Called from inside `sessionTask`, so unlike `stop` it must
    /// not await it.
    private func abort() {
        guard isListening else { return }
        isListening = false
        closeMic()
        resultsTask?.cancel()
        resultsTask = nil
        analyzer = nil
        module = nil
        deactivateSession()
    }

    private func closeMic() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        capture?.finish()
        capture = nil
        endpointer = nil
    }

    private func deactivateSession() {
        try? AVAudioSession.sharedInstance().setActive(
            false, options: .notifyOthersOnDeactivation
        )
    }

    // MARK: - Audio

    private func startEngine(into capture: AsyncStream<CapturedAudio>.Continuation) throws {
        try Self.configureSession()
        try AVAudioSession.sharedInstance().setActive(true)

        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)
        let endpointer = self.endpointer

        // This closure runs on a realtime audio thread. It touches nothing on
        // the main actor — only the continuation and the endpointer, which is
        // measured against the *input* buffer: it is always float32 from the
        // input node, whereas the analyzer's preferred format need not be.
        //
        // The resample that used to live here moved to `feed`, because the
        // analyzer's format isn't known yet when the tap goes on. What replaces
        // it is one copy: `installTap` only guarantees its buffer for the
        // length of the call, and it is now read on another thread afterwards.
        // Same single allocation per callback either way.
        input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { buffer, _ in
            endpointer?.ingest(buffer)
            if let copy = buffer.handoffCopy() {
                capture.yield(CapturedAudio(buffer: copy))
            }
        }

        engine.prepare()
        try engine.start()
    }

    /// Pumps captured audio into the analyzer, resampling on the way.
    ///
    /// `nonisolated` so the conversion runs off the main actor, and off the
    /// audio thread — this is the one place in the path where taking a moment
    /// costs a late transcript rather than a dropout.
    private nonisolated static func feed(
        _ captured: AsyncStream<CapturedAudio>,
        into input: AsyncStream<AnalyzerInput>.Continuation,
        resampling target: AVAudioFormat?
    ) async {
        let resampler = Resampler()
        for await item in captured {
            guard let target else {
                input.yield(AnalyzerInput(buffer: item.buffer))
                continue
            }
            if let converted = resampler.convert(item.buffer, to: target) {
                input.yield(AnalyzerInput(buffer: converted))
            }
        }
        input.finish()
    }

    /// Category only. Activating the session here rather than in `start` would
    /// duck whatever else is playing for as long as the app is open.
    ///
    /// .playAndRecord, not .record: the reply is spoken the moment capture
    /// ends, and swapping categories in between clips the first syllable.
    private static func configureSession() throws {
        let session = AVAudioSession.sharedInstance()
        let options: AVAudioSession.CategoryOptions =
            [.duckOthers, .defaultToSpeaker, .allowBluetoothHFP]
        guard session.category != .playAndRecord
            || session.mode != .spokenAudio
            || session.categoryOptions != options
        else { return }
        try session.setCategory(.playAndRecord, mode: .spokenAudio, options: options)
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

    // MARK: - Warm-up

    /// The speech stack's fixed costs, paid once per launch.
    ///
    /// Resolving the locale, confirming the model is installed and asking for
    /// the analyzer's preferred format are three round trips to the speech
    /// daemon that return the same answers every time. The first caller pays;
    /// everyone after reads.
    private struct Warm {
        let locale: Locale
        /// `nil` means the analyzer takes the mic's own format, so nothing
        /// needs resampling.
        let format: AVAudioFormat?
    }

    private static var warming: Task<Warm, Error>?

    private static func warmed(reporting listener: Transcriber?) async throws -> Warm {
        if let warming { return try await warming.value }

        let task = Task { [weak listener] in try await computeWarm(reporting: listener) }
        warming = task
        do {
            return try await task.value
        } catch {
            // A failed warm-up is not a permanent answer — the model may simply
            // not have downloaded yet — so the next caller tries again rather
            // than inheriting this one's failure for the life of the process.
            warming = nil
            throw error
        }
    }

    private static func computeWarm(reporting listener: Transcriber?) async throws -> Warm {
        let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale.current)
            ?? Locale(identifier: "en-US")
        let module = SpeechTranscriber(locale: locale, preset: .progressiveTranscription)
        try await installModelIfNeeded(for: module, locale: locale, reporting: listener)
        let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [module])
        return Warm(locale: locale, format: format)
    }

    /// Whether the mic may be opened ahead of the analyzer.
    ///
    /// Persisted rather than per-launch: what it really records is that this
    /// device has a speech model on it, and a launch that might have to
    /// download one has to finish that before it opens a mic.
    private static var hasTranscribedBefore: Bool {
        get { UserDefaults.standard.bool(forKey: "transcriber.modelReady") }
        set { UserDefaults.standard.set(newValue, forKey: "transcriber.modelReady") }
    }

    // MARK: - Model assets

    private static func installModelIfNeeded(
        for module: SpeechTranscriber,
        locale: Locale,
        reporting listener: Transcriber?
    ) async throws {
        if await AssetInventory.status(forModules: [module]) == .installed { return }

        listener?.status = "Downloading the speech model…"
        defer { listener?.status = "" }

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

/// One tap callback's worth of audio, in transit from the audio thread to the
/// analyzer.
///
/// The unchecked conformance is true rather than merely convenient: the audio
/// thread copies the tap's buffer, hands the copy over, and never looks at it
/// again.
private struct CapturedAudio: @unchecked Sendable {
    let buffer: AVAudioPCMBuffer
}

private extension AVAudioPCMBuffer {
    /// A copy that outlives the tap callback.
    ///
    /// Written against the audio buffer list rather than `floatChannelData` so
    /// it holds for interleaved and non-float input too — the input node's
    /// format is not ours to choose.
    func handoffCopy() -> AVAudioPCMBuffer? {
        guard frameLength > 0,
              let copy = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameLength)
        else { return nil }
        copy.frameLength = frameLength

        let source = UnsafeMutableAudioBufferListPointer(mutableAudioBufferList)
        let destination = UnsafeMutableAudioBufferListPointer(copy.mutableAudioBufferList)
        for index in 0..<min(source.count, destination.count) {
            guard let from = source[index].mData, let to = destination[index].mData else {
                return nil
            }
            to.copyMemory(
                from: from,
                byteCount: Int(min(source[index].mDataByteSize, destination[index].mDataByteSize))
            )
        }
        return copy
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
