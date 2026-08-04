import SwiftUI

/// Answering a report that asked you something.
///
/// The reply re-queues the report rather than starting a new one, so this is
/// deliberately not a chat: there is no transcript above it, because the
/// resumed run rewrites the report you are already looking at. Send, and the
/// detail view's poll shows it change.
///
/// The mic is the same `Transcriber` the talk screen uses. Speaking and typing
/// are one code path with two ways in — an answer is a sentence either way.
struct ReplyBox: View {
    let jobID: Int
    /// Queued or running: the server would 409, so don't offer it.
    let isLive: Bool
    let onSent: () -> Void

    @EnvironmentObject private var api: JarvisAPI
    @EnvironmentObject private var toasts: ToastCenter
    @StateObject private var transcriber = Transcriber()

    @State private var text = ""
    @State private var isSending = false
    @FocusState private var focused: Bool

    private var canSend: Bool {
        !isLive && !isSending && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("REPLY")
                .font(Theme.mono(11))
                .tracking(0.6)
                .foregroundStyle(Theme.text3)

            HStack(alignment: .bottom, spacing: 8) {
                TextField(
                    isLive ? "Working…" : "Answer, and it picks up where it left off",
                    text: $text,
                    axis: .vertical
                )
                .font(Theme.sans(14.5))
                .foregroundStyle(Theme.text)
                .lineLimit(1...5)
                .focused($focused)
                .disabled(isLive || isSending)
                .padding(10)
                .background(Theme.surface2, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay {
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .strokeBorder(Theme.border, lineWidth: 1)
                }

                micButton
                sendButton
            }

            if transcriber.isListening {
                Text("Listening… tap the mic to stop.")
                    .font(Theme.sans(12))
                    .foregroundStyle(Theme.text3)
            }
        }
        .task { transcriber.prepare() }
        .onChange(of: transcriber.transcript) { _, spoken in
            // Live transcript, so you can watch it land in the field rather
            // than waiting for the whole utterance to resolve.
            if transcriber.isListening { text = spoken }
        }
    }

    private var micButton: some View {
        Button {
            Task { await toggleMic() }
        } label: {
            Image(systemName: transcriber.isListening ? "stop.fill" : "mic.fill")
                .font(.system(size: 14))
                .foregroundStyle(transcriber.isListening ? Theme.accent : Theme.text3)
                .frame(width: 38, height: 38)
                .background(Theme.surface2, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay {
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .strokeBorder(Theme.border, lineWidth: 1)
                }
        }
        .buttonStyle(.plain)
        .disabled(isLive || isSending)
        .accessibilityLabel(transcriber.isListening ? "Stop dictating" : "Dictate a reply")
    }

    private var sendButton: some View {
        Button {
            Task { await send() }
        } label: {
            Image(systemName: "arrow.up")
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(canSend ? Theme.bg : Theme.text3)
                .frame(width: 38, height: 38)
                .background(
                    canSend ? Theme.accent : Theme.surface2,
                    in: RoundedRectangle(cornerRadius: 10, style: .continuous)
                )
        }
        .buttonStyle(.plain)
        .disabled(!canSend)
        .accessibilityLabel("Send reply")
    }

    private func toggleMic() async {
        if transcriber.isListening {
            text = await transcriber.stop()
            return
        }
        // No pauseToSend: a reply is reviewed before it goes, unlike the talk
        // screen where the pause *is* the send.
        try? await transcriber.start(pauseToSend: nil)
    }

    private func send() async {
        let outgoing = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !outgoing.isEmpty else { return }
        isSending = true
        defer { isSending = false }
        focused = false
        do {
            _ = try await api.reply(toJob: jobID, text: outgoing)
            text = ""
            toasts.show("Sent. It's picking that up.")
            onSent()
        } catch {
            // The reply stays in the field — a dropped answer you have to
            // retype is the one failure this feature cannot afford.
            toasts.show(Failure.reason(error.localizedDescription))
        }
    }
}
