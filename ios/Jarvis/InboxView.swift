import SwiftUI

/// Ingested mail, as context.
///
/// Not a mail client. There is no reply, no compose, no body view — and not
/// because those were cut for time. Message bodies are never stored: Gmail is
/// fetched with `format=metadata`, which means Google does not return them, so
/// there is no path to storing one by accident. That is a privacy guarantee
/// baked into the architecture, and the honest thing is to say so on the screen
/// rather than build something that looks like it is hiding the body.
///
/// What it is for: checking what the assistant is working from. "Did the
/// landlord ever write back?" is answerable because these rows exist.
struct InboxView: View {
    @EnvironmentObject private var api: JarvisAPI

    @State private var messages: [InboxResponse.Message]?
    @State private var query = ""
    @State private var unreadOnly = false
    @State private var error: String?
    @State private var isLoading = false

    var body: some View {
        VStack(spacing: 0) {
            filterBar

            if messages == nil, let error {
                ScrollView {
                    ErrorState(
                        title: "Can't reach the Mini",
                        detail: Failure.reason(error),
                        hint: "Usually means the private network (Tailscale) is off.",
                        retry: { Task { await load() } }
                    )
                    .padding(.top, 60)
                }
            } else if messages == nil {
                Spacer()
                ProgressView().tint(Theme.accent)
                Spacer()
            } else if let messages {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        if let error {
                            ErrorBanner(message: error).padding(.bottom, 12)
                        }

                        Text("Metadata and Google's own snippet only — message bodies are never stored.")
                            .font(Theme.sans(11.5))
                            .foregroundStyle(Theme.text3)
                            .padding(.bottom, 12)

                        if messages.isEmpty {
                            EmptyState(
                                title: query.isEmpty ? "No mail" : "Nothing matches",
                                message: query.isEmpty
                                    ? "Nothing has been ingested yet."
                                    : "No ingested message matches that."
                            )
                        }

                        ForEach(messages) { message in
                            MessageRow(message: message)
                        }
                    }
                    .padding(.horizontal, 20)
                    .padding(.bottom, 24)
                }
                .refreshable { await load() }
            }
        }
        .jarvisBackground()
        .navigationTitle("Inbox")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        // Debounced rather than per-keystroke: `?q=` runs an FTS query on the
        // Mini, and a search-as-you-type at 60 requests a sentence is rude to a
        // machine that is also serving the fast path.
        .task(id: query) {
            try? await Task.sleep(for: .milliseconds(300))
            guard !Task.isCancelled else { return }
            await load()
        }
        .task(id: unreadOnly) { await load() }
    }

    private var filterBar: some View {
        HStack(spacing: 10) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.text3)
                TextField("Search subjects and snippets", text: $query)
                    .font(Theme.sans(14))
                    .foregroundStyle(Theme.text)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                if !query.isEmpty {
                    Button {
                        query = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 13))
                            .foregroundStyle(Theme.text3)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .background(Theme.surface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(Theme.border, lineWidth: 1)
            }

            Button {
                unreadOnly.toggle()
            } label: {
                Text("Unread")
                    .font(Theme.sans(13, weight: unreadOnly ? .semibold : .regular))
                    .padding(.horizontal, 12)
                    .padding(.vertical, 9)
                    .background(
                        unreadOnly ? Theme.accent : Theme.surface,
                        in: RoundedRectangle(cornerRadius: 10, style: .continuous)
                    )
                    .foregroundStyle(unreadOnly ? Theme.onAccent : Theme.text2)
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            messages = try await api.inbox(
                limit: 50, unreadOnly: unreadOnly, query: query
            ).messages
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct MessageRow: View {
    let message: InboxResponse.Message

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                if message.unread {
                    Circle()
                        .fill(Theme.accent)
                        .frame(width: 6, height: 6)
                        .offset(y: -1)
                }
                Text(message.sender ?? "Unknown sender")
                    .font(Theme.sans(14, weight: .semibold))
                    .foregroundStyle(Theme.text)
                Spacer(minLength: 8)
                if let received = message.receivedAt {
                    Text(RelativeStamp.short(received))
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.text3)
                }
            }

            Text(message.subject ?? "(no subject)")
                .font(Theme.sans(13.5))
                .foregroundStyle(Theme.text2)

            if let snippet = message.snippet, !snippet.isEmpty {
                Text(snippet)
                    .font(Theme.sans(12.5))
                    .foregroundStyle(Theme.text3)
                    .lineLimit(2)
            }
        }
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(alignment: .bottom) { Theme.border.frame(height: 1) }
    }
}
