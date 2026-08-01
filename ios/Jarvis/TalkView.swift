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
/// control at the bottom, and why the phase is carried by the mic's colour
/// rather than by a label you would have to read.
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
        // Two hooks, not one: the intent may set the latch before this
        // view exists (cold launch) or after it is already on screen
        // (app already running). `task` catches the first, `onChange` the
        // second, and the latch is read-and-clear so neither double-fires.
        .task { await startIfRequested() }
        .onChange(of: router.shouldStartListening) { _, _ in
            Task { await startIfRequested() }
        }
        // You stopped talking. Same path as tapping the button.
        .onChange(of: transcriber.didEndpoint) { _, reached in
            guard reached else { return }
            Task { await finish() }
        }
    }

    // MARK: - Stage

    @ViewBuilder
    private var stage: some View {
        VStack(spacing: 18) {
            switch phase {
            case .idle:
                Text("Tap the mic — or press the Action Button — to talk to Jarvis.")
                    .font(Theme.sans(15))
                    .foregroundStyle(Theme.text3)

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

    private var micButton: some View {
        Button {
            Task { await toggle() }
        } label: {
            ZStack {
                Circle()
                    .fill(
                        RadialGradient(
                            colors: transcriber.isListening
                                ? [Color(oklch: (0.75, 0.19, 25)), Theme.danger]
                                : [Color(oklch: (0.80, 0.17, 231)), Theme.accent],
                            center: .init(x: 0.32, y: 0.28),
                            startRadius: 2,
                            endRadius: 90
                        )
                    )
                    .overlay(Circle().strokeBorder(Color.white.opacity(0.25), lineWidth: 1))
                    .frame(width: 92, height: 92)

                if isSending {
                    ProgressView().tint(.white)
                } else {
                    Image(systemName: transcriber.isListening ? "stop.fill" : "mic.fill")
                        .font(.system(size: 32))
                        .foregroundStyle(.white)
                }
            }
            .background {
                MicHalo(
                    tint: transcriber.isListening ? Theme.danger : Theme.accent,
                    active: !isSending
                )
            }
        }
        .buttonStyle(.plain)
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

        let text = await transcriber.stop()
        await send(text)
    }

    private func send(_ text: String) async {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }

        isSending = true
        defer { isSending = false }

        do {
            let response = try await api.say(trimmed)
            reply = response.reply
            route = response.route
            latencyMs = response.latencyMs
            jobID = response.jobId
            speaker.speak(response.reply)
        } catch {
            self.error = error.localizedDescription
        }
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

/// The ring behind the mic: a slow breath when idle, an urgent pulse while
/// listening. It is the only thing on screen that says which of those two the
/// app is in without being read.
private struct MicHalo: View {
    let tint: Color
    let active: Bool

    @State private var expanded = false

    var body: some View {
        Circle()
            .strokeBorder(tint.opacity(expanded ? 0 : 0.45), lineWidth: 6)
            .frame(width: 92, height: 92)
            .scaleEffect(expanded ? 1.35 : 1.0)
            .animation(
                active
                    ? .easeOut(duration: 1.6).repeatForever(autoreverses: false)
                    : .default,
                value: expanded
            )
            .onAppear { expanded = true }
            .onChange(of: active) { _, on in expanded = on }
            .accessibilityHidden(true)
    }
}
