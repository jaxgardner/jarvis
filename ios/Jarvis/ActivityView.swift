import SwiftUI

/// Everything you've said, and what it changed.
///
/// This screen exists because of principle 4: voice input is lossy, so undo is
/// load-bearing. The mutations log has recorded every write from the start,
/// but
/// nothing surfaced it — "undo that" was an act of faith. Here you can see what
/// would be reversed before reversing it.
struct ActivityView: View {
    @EnvironmentObject private var api: JarvisAPI

    @State private var utterances: [ActivityResponse.Utterance] = []
    @State private var error: String?
    @State private var isLoading = false

    var body: some View {
        NavigationStack {
            List {
                if let error {
                    Text(error).foregroundStyle(.red).font(.footnote)
                }

                if utterances.isEmpty && !isLoading {
                    ContentUnavailableView(
                        "Nothing yet",
                        systemImage: "waveform",
                        description: Text("Things you say to Jarvis show up here.")
                    )
                    .listRowSeparator(.hidden)
                }

                ForEach(utterances) { utterance in
                    entry(utterance)
                        // Only the newest undoable row gets the gesture. /undo
                        // reverses the most recent mutation and nothing else,
                        // so a swipe on an older row would quietly reverse
                        // something you weren't looking at.
                        .swipeActions(edge: .trailing) {
                            if utterance.isUndoable {
                                Button(role: .destructive) {
                                    Task { await undo() }
                                } label: {
                                    Label("Undo", systemImage: "arrow.uturn.backward")
                                }
                            }
                        }
                }
            }
            .listStyle(.plain)
            .navigationTitle("Activity")
            .refreshable { await load() }
            .task { await load() }
            .overlay { if isLoading && utterances.isEmpty { ProgressView() } }
        }
    }

    private func entry(_ utterance: ActivityResponse.Utterance) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(utterance.rawText)
                .font(.body)
                .strikethrough(utterance.wasUndone, color: .secondary)
                .foregroundStyle(utterance.wasUndone ? .secondary : .primary)

            if let reply = utterance.responseText, !reply.isEmpty {
                Text(reply)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }

            HStack(spacing: 6) {
                if let intent = utterance.intent {
                    Tag(text: intent, tint: utterance.route == "deep" ? .purple : .blue)
                }
                if let ms = utterance.latencyMs {
                    // Over budget is worth seeing on the row, not just in a
                    // percentile you have to go looking for.
                    Tag(text: "\(ms) ms", tint: ms > 2000 ? .red : .secondary)
                }
                if utterance.wasUndone {
                    Tag(text: "undone", tint: .secondary)
                }
                if utterance.isUndoable {
                    Tag(text: "swipe to undo", tint: .orange)
                }
            }
        }
        .padding(.vertical, 4)
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            utterances = try await api.activity().utterances
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func undo() async {
        do {
            try await api.undo()
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

/// Small pill used across the dashboard.
struct Tag: View {
    let text: String
    var tint: Color = .secondary

    var body: some View {
        Text(text)
            .font(.caption2.weight(.medium))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(tint.opacity(0.15), in: Capsule())
            .foregroundStyle(tint)
    }
}
