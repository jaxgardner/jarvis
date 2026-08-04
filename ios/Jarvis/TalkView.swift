import OSLog
import SwiftUI

/// The screen that has to replace the Shortcut. Tap, talk, done.
///
/// One thing deliberately absent: a Send button. The Shortcut's whole
/// ergonomic advantage is that it is one gesture, and adding a confirmation
/// step would make this slower than the thing it replaces. The second tap is
/// gone for the same reason — stopping talking is the signal, and the mic
/// button stays as the manual override for a noisy room.
///
/// The Action Button opens this screen already listening, which sets the bar
/// for the layout: it has to be legible mid-sentence, before you have looked
/// at it. That is why there is one thing in the middle of the screen and one
/// control at the bottom, and why the phase is carried by the orb's colour and
/// motion rather than by a label you would have to read. `MicOrb` is that
/// control.
struct TalkView: View {
    @EnvironmentObject private var api: JarvisAPI
    @StateObject private var transcriber = Transcriber()
    @StateObject private var speaker = Speaker()
    @ObservedObject private var router = LaunchRouter.shared

    @AppStorage(VoiceSettings.autoSendKey) private var autoSend = true
    @AppStorage(VoiceSettings.pauseKey) private var pauseToSend = VoiceSettings.defaultPause

    @State private var reply = ""
    @State private var route = ""
    @State private var latencyMs: Int?
    @State private var jobID: Int?
    @State private var isSending = false
    @State private var isFinishing = false
    @State private var error: String?
    @State private var showingSettings = false

    /// When the endpointer decided you had stopped talking. Everything after
    /// this instant is latency the user is sitting through, and only about
    /// half of it is inside /say — which is the whole reason the server's
    /// `latency_ms` was not enough to work from.
    @State private var turnStart: ContinuousClock.Instant?
    /// When the first buffer of the reply started playing. Written by
    /// `Speaker.onFirstAudio` while `speak` is still running.
    @State private var firstAudioAt: ContinuousClock.Instant?

    /// The turn as one timeline in Instruments.
    ///
    /// `turn_ms` says a turn was slow; these say which hop it was slow in,
    /// which is the question the single number cannot answer. Signposts
    /// rather than logging because the hops nest and overlap, and because
    /// they cost nothing when nothing is recording.
    private static let signposter = OSSignposter(
        subsystem: "com.jarvis", category: "turn"
    )

    private enum Phase { case idle, listening, sending, reply, error }

    private var phase: Phase {
        if error != nil { return .error }
        if isSending { return .sending }
        if transcriber.isListening { return .listening }
        return reply.isEmpty ? .idle : .reply
    }

    var body: some View {
        VStack(spacing: 0) {
            ScreenHeader(title: "Talk", kicker: "Voice · private") {
                Button {
                    showingSettings = true
                } label: {
                    Image(systemName: "gearshape")
                        .font(.system(size: 15))
                        .foregroundStyle(Theme.text2)
                        .frame(width: 34, height: 34)
                        .background(
                            LinearGradient(
                                colors: [Theme.surface2, Theme.surface],
                                startPoint: .top,
                                endPoint: .bottom
                            ),
                            in: Circle()
                        )
                        .overlay(Circle().strokeBorder(Theme.border, lineWidth: 1))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Settings")
            }

            Spacer(minLength: 0)

            stage
                .padding(.horizontal, 28)
                .frame(maxWidth: .infinity)

            Spacer(minLength: 0)

            micButton
                .padding(.top, 26)
                .padding(.bottom, 34)
        }
        .sheet(isPresented: $showingSettings) {
            SettingsView()
        }
        // The speech stack's fixed costs, paid while you are looking at the
        // screen rather than after you have pressed the button. Talk is the
        // default tab, so in practice this runs at launch.
        .task { transcriber.prepare() }
        // Two hooks, not one: the intent may set the latch before this
        // view exists (cold launch) or after it is already on screen
        // (app already running). `task` catches the first, `onChange` the
        // second, and the latch is read-and-clear so neither double-fires.
        .task {
            speaker.source = api
            // Stops the turn clock. Set here rather than per-utterance so
            // there is one assignment and no closure capturing a stale id.
            speaker.onFirstAudio = { firstAudioAt = .now }
            await startIfRequested()
        }
        .onChange(of: router.shouldStartListening) { _, _ in
            Task { await startIfRequested() }
        }
        // The mic opens before the analyzer does, so a speech stack that fails
        // to come up fails after `start` has already returned.
        .onChange(of: transcriber.failure) { _, message in
            if let message { error = message }
        }
        // You stopped talking. Same path as tapping the button.
        .onChange(of: transcriber.didEndpoint) { _, reached in
            guard reached else { return }
            turnStart = .now
            Task { await finish() }
        }
    }

    // MARK: - Stage

    @ViewBuilder
    private var stage: some View {
        VStack(spacing: 18) {
            switch phase {
            case .idle:
                // A greeting, not an instruction. What was here explained how
                // to work the button, which is worth exactly one reading and is
                // then dead text on the screen you open most. An assistant that
                // is standing by should say so — and it is set in the reply's
                // own type, a shade under its weight, because it is the same
                // voice speaking either way.
                Text("Hello, Sir.")
                    .font(Theme.sans(24, weight: .medium))
                    .foregroundStyle(Theme.text2)
                    .tracking(-0.2)

            case .listening:
                ListeningBars()
                Text(transcriber.transcript)
                    .font(Theme.sans(17))
                    .foregroundStyle(Theme.text2)
                    .frame(minHeight: 52, alignment: .top)

            case .sending:
                Text(transcriber.transcript)
                    .font(Theme.sans(17))
                    .foregroundStyle(Theme.text2)
                ProgressView()
                    .tint(Theme.accent)

            case .reply:
                // The reply is a spoken sentence, not a card to re-parse. The
                // server guarantees plain TTS-safe text, so it is presented as
                // one line of speech and nothing more.
                Text(reply)
                    .font(Theme.sans(24, weight: .semibold))
                    .foregroundStyle(Theme.text)
                    .tracking(-0.2)
                if let caption {
                    Text(caption)
                        .font(Theme.mono(13))
                        .foregroundStyle(captionTint)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 5)
                        .background(Theme.surface2, in: Capsule())
                }

            case .error:
                ErrorState(
                    title: error ?? "Something went wrong.",
                    hint: "Usually means the private network (Tailscale) is off."
                )
            }

            if !transcriber.status.isEmpty {
                Text(transcriber.status)
                    .font(Theme.sans(12))
                    .foregroundStyle(Theme.text3)
            }
        }
        .multilineTextAlignment(.center)
        .animation(.easeInOut(duration: 0.2), value: reply)
        .animation(.easeInOut(duration: 0.2), value: isSending)
    }

    /// `fast · 580 ms` or `deep · 640 ms · job 27`. On screen after every
    /// utterance because a budget nobody looks at is a budget nobody keeps.
    private var caption: String? {
        guard !route.isEmpty else { return nil }
        var parts = [route]
        if let latencyMs { parts.append("\(latencyMs) ms") }
        if let jobID { parts.append("job \(jobID)") }
        return parts.joined(separator: " · ")
    }

    private var captionTint: Color {
        if let latencyMs, latencyMs > Theme.latencyBudgetMs { return Theme.danger }
        return route == "deep" ? Theme.deep : Theme.text3
    }

    // MARK: - Mic

    private var orbPhase: MicOrb.Phase {
        if isSending { return .sending }
        return transcriber.isListening ? .listening : .idle
    }

    private var micButton: some View {
        Button {
            Task { await toggle() }
        } label: {
            MicOrb(phase: orbPhase)
        }
        .buttonStyle(MicOrbButtonStyle())
        .disabled(isSending)
        .sensoryFeedback(.impact, trigger: transcriber.isListening)
        // "Send now", not "Stop listening": tapping while listening has always
        // sent, and with the pause detector on it is specifically the way to
        // send before the pause elapses.
        .accessibilityLabel(transcriber.isListening ? "Send now" : "Talk to Jarvis")
    }

    // MARK: - Flow

    private func startIfRequested() async {
        guard router.consumeListeningRequest(), !transcriber.isListening else { return }
        await toggle()
    }

    private func toggle() async {
        error = nil
        if transcriber.isListening {
            await finish()
        } else {
            speaker.stop()
            reply = ""
            route = ""
            latencyMs = nil
            jobID = nil
            do {
                try await transcriber.start(pauseToSend: autoSend ? pauseToSend : nil)
            } catch {
                self.error = error.localizedDescription
            }
        }
    }

    /// Stop capturing and send. Reachable two ways — the pause detector and
    /// the button — which can land within milliseconds of each other when you
    /// tap just as you finish speaking. `isFinishing` is set before the first
    /// suspension point, so the loser of that race returns instead of posting
    /// the same utterance twice.
    private func finish() async {
        guard transcriber.isListening, !isFinishing else { return }
        isFinishing = true
        defer { isFinishing = false }

        let stopping = Self.signposter.beginInterval("endpoint-to-stop")
        let text = await transcriber.stop()
        Self.signposter.endInterval("endpoint-to-stop", stopping)

        await send(text)
    }

    private func send(_ text: String) async {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }

        isSending = true
        defer { isSending = false }

        firstAudioAt = nil
        do {
            let saying = Self.signposter.beginInterval("say")
            let response = try await api.say(trimmed)
            Self.signposter.endInterval("say", saying)

            reply = response.reply
            route = response.route
            latencyMs = response.latencyMs
            jobID = response.jobId

            // Covers the request for audio and the wait for the first buffer.
            // `turn_ms` is the one number the goal is stated in; these are for
            // the morning it is wrong and the reason is not obvious.
            let speaking = Self.signposter.beginInterval("say-returned-to-first-audio")
            await speaker.speak(response.reply)
            Self.signposter.endInterval("say-returned-to-first-audio", speaking)

            await reportTurn(for: response.utteranceId)
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// After playback has started, never before: this is a measurement, and
    /// one that delayed the thing it measures would be worse than none.
    ///
    /// Nothing is reported when the turn began with the mic button rather
    /// than the endpointer, or when no audio ever played. A turn nobody
    /// waited through in the usual way is not the number the goal is stated
    /// in, and a zero folded in would flatter it.
    private func reportTurn(for utteranceId: Int) async {
        guard let started = turnStart, let heard = firstAudioAt else { return }
        turnStart = nil

        let elapsed = heard - started
        let milliseconds =
            elapsed.components.seconds * 1000
            + elapsed.components.attoseconds / 1_000_000_000_000_000
        await api.reportTurn(utteranceId: utteranceId, turnMs: Int(milliseconds))
    }
}

/// Five bars, staggered. Not audio-reactive — the transcriber publishes no
/// level, and inventing one that doesn't track the room would be worse than a
/// motion that plainly means "the mic is open".
private struct ListeningBars: View {
    @State private var extended = false

    private static let bars: [(scale: CGFloat, duration: Double, delay: Double)] = [
        (0.70, 0.90, 0.00),
        (1.00, 0.70, 0.10),
        (0.50, 0.80, 0.20),
        (0.90, 0.60, 0.05),
        (0.60, 0.75, 0.15),
    ]

    var body: some View {
        HStack(spacing: 6) {
            ForEach(Self.bars.indices, id: \.self) { index in
                let bar = Self.bars[index]
                Capsule()
                    .fill(
                        LinearGradient(
                            colors: [Theme.accent, Theme.accent.opacity(0.4)],
                            startPoint: .top,
                            endPoint: .bottom
                        )
                    )
                    .frame(width: 4, height: 40 * bar.scale)
                    .scaleEffect(y: extended ? 1.0 : 0.28, anchor: .center)
                    .animation(
                        .easeInOut(duration: bar.duration)
                            .repeatForever(autoreverses: true)
                            .delay(bar.delay),
                        value: extended
                    )
            }
        }
        .frame(height: 40)
        .onAppear { extended = true }
        .accessibilityHidden(true)
    }
}

