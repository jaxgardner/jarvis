import SwiftUI

/// The screen that has to replace the Shortcut. Tap, talk, done.
///
/// One thing deliberately absent: a Send button. The Shortcut's whole
/// ergonomic advantage is that it is one gesture, and adding a confirmation
/// step would make this slower than the thing it replaces. The second tap is
/// gone for the same reason — stopping talking is the signal, and the mic
/// button stays as the manual override for a noisy room.
struct TalkView: View {
    @EnvironmentObject private var api: JarvisAPI
    @StateObject private var transcriber = Transcriber()
    @StateObject private var speaker = Speaker()
    @ObservedObject private var router = LaunchRouter.shared

    @AppStorage(VoiceSettings.autoSendKey) private var autoSend = true
    @AppStorage(VoiceSettings.pauseKey) private var pauseToSend = VoiceSettings.defaultPause

    @State private var reply = ""
    @State private var detail = ""
    @State private var isSending = false
    @State private var isFinishing = false
    @State private var error: String?
    @State private var showingSettings = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 24) {
                Spacer()

                transcriptArea

                Spacer()

                if let error {
                    Text(error)
                        .font(.footnote)
                        .foregroundStyle(.red)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal)
                }

                micButton
                    .padding(.bottom, 40)
            }
            .frame(maxWidth: .infinity)
            .navigationTitle("Jarvis")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showingSettings = true
                    } label: {
                        Image(systemName: "gearshape")
                    }
                }
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
    }

    private func startIfRequested() async {
        guard router.consumeListeningRequest(), !transcriber.isListening else { return }
        await toggle()
    }

    private var transcriptArea: some View {
        VStack(spacing: 16) {
            if !transcriber.transcript.isEmpty {
                Text(transcriber.transcript)
                    .font(.title3)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }

            if !reply.isEmpty {
                Text(reply)
                    .font(.title2.weight(.medium))
                    .multilineTextAlignment(.center)
                    .transition(.opacity)
            }

            if !detail.isEmpty {
                // Latency is on screen because p95 > 2s is a bug, and a budget
                // you never look at is a budget you never keep.
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }

            if !transcriber.status.isEmpty {
                Text(transcriber.status)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 32)
        .animation(.easeInOut(duration: 0.2), value: reply)
    }

    private var micButton: some View {
        Button {
            Task { await toggle() }
        } label: {
            ZStack {
                Circle()
                    .fill(transcriber.isListening ? .red : .accentColor)
                    .frame(width: 96, height: 96)
                if isSending {
                    ProgressView()
                        .tint(.white)
                } else {
                    Image(systemName: transcriber.isListening ? "stop.fill" : "mic.fill")
                        .font(.system(size: 36))
                        .foregroundStyle(.white)
                }
            }
        }
        .disabled(isSending)
        .sensoryFeedback(.impact, trigger: transcriber.isListening)
        // "Send now", not "Stop listening": tapping while listening has always
        // sent, and with the pause detector on it is specifically the way to
        // send before the pause elapses.
        .accessibilityLabel(transcriber.isListening ? "Send now" : "Talk to Jarvis")
    }

    private func toggle() async {
        error = nil
        if transcriber.isListening {
            await finish()
        } else {
            speaker.stop()
            reply = ""
            detail = ""
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
            detail = Self.describe(response)
            speaker.speak(response.reply)
        } catch {
            self.error = error.localizedDescription
        }
    }

    private static func describe(_ response: SayResponse) -> String {
        var parts = [response.route]
        if let ms = response.latencyMs { parts.append("\(ms) ms") }
        if let job = response.jobId { parts.append("job \(job)") }
        return parts.joined(separator: " · ")
    }
}
