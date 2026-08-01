import SwiftUI

/// Everything you've said, and what it changed.
///
/// This screen exists because of principle 4: voice input is lossy, so undo is
/// load-bearing. The mutations log has recorded every write from the start, but
/// nothing surfaced it — "undo that" was an act of faith. Here you can see what
/// would be reversed before reversing it.
struct ActivityView: View {
    @EnvironmentObject private var api: JarvisAPI
    @EnvironmentObject private var toasts: ToastCenter

    @State private var utterances: [ActivityResponse.Utterance]?
    @State private var error: String?
    @State private var isLoading = false

    var body: some View {
        VStack(spacing: 0) {
            ScreenHeader(title: "Activity", kicker: "What you said · what it changed")
            RefreshStamp(isLoading: isLoading, failed: error != nil)
            content
        }
        .task { await load() }
    }

    @ViewBuilder
    private var content: some View {
        if utterances == nil, let error {
            ScrollView {
                ErrorState(
                    title: "Can't reach the Mini",
                    detail: Failure.reason(error),
                    hint: "Usually means the private network (Tailscale) is off.",
                    retry: { Task { await load() } }
                )
                .padding(.top, 60)
            }
            .refreshable { await load() }
        } else if utterances == nil {
            Spacer()
            ProgressView().tint(Theme.accent)
            Spacer()
        } else if let utterances {
            List {
                if let error {
                    ErrorBanner(message: error).plainRow()
                }

                if utterances.isEmpty {
                    EmptyState(
                        title: "Nothing yet",
                        message: "Things you say to Jarvis show up here."
                    )
                    .plainRow()
                }

                ForEach(utterances) { utterance in
                    UtteranceCard(utterance: utterance)
                        .plainRow()
                        // Only the newest undoable row gets the gesture. /undo
                        // reverses the most recent mutation and nothing else,
                        // so a swipe on an older row would quietly reverse
                        // something you weren't looking at.
                        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                            if utterance.isUndoable {
                                Button(role: .destructive) {
                                    Task { await undo() }
                                } label: {
                                    Label("Undo", systemImage: "arrow.uturn.backward")
                                }
                                .tint(Theme.danger)
                            }
                        }
                }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .refreshable { await load() }
        }
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
            let result = try await api.undo()
            toasts.show(result["reply"] ?? "Undone")
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct UtteranceCard: View {
    let utterance: ActivityResponse.Utterance

    /// The deep path is purple everywhere it appears. Constraint 8: the two
    /// paths must read as different things, and colour is the cheapest way to
    /// say so on a row you are scanning rather than reading.
    private var routeTint: Color {
        utterance.route == "deep" ? Theme.deep : Theme.accent
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(utterance.rawText)
                .font(Theme.sans(14.5, weight: .semibold))
                .strikethrough(utterance.wasUndone, color: Theme.text3)
                .foregroundStyle(utterance.wasUndone ? Theme.text3 : Theme.text)

            if let reply = utterance.responseText, !reply.isEmpty {
                Text(reply)
                    .font(Theme.sans(13))
                    .foregroundStyle(Theme.text2)
            }

            HStack(spacing: 6) {
                if let intent = utterance.intent {
                    Pill(text: intent, tint: routeTint)
                }
                if let ms = utterance.latencyMs {
                    // Over budget is worth seeing on the row, not just in a
                    // percentile you have to go looking for.
                    let over = ms > Theme.latencyBudgetMs
                    Pill(text: "\(ms) ms", tint: over ? Theme.danger : Theme.text2, emphasised: over)
                }
                // The design shows a `job 27` pill here. /activity can't
                // supply it: escalate inserts into `jobs` directly rather than
                // through the mutations helper, so no mutation row points at
                // the job. The purple intent pill carries "this went deep"
                // until the endpoint carries the id.
                if utterance.wasUndone {
                    Pill(text: "undone", tint: Theme.text3, emphasised: false)
                }
                if utterance.isUndoable {
                    // The one row /undo would take back. Saying which is the
                    // whole point — the endpoint reverses the newest change and
                    // nothing else.
                    Pill(text: "swipe to undo", tint: Theme.accent)
                }
            }
            .padding(.top, 5)
        }
        .jarvisCard(radius: 12, padding: 12)
    }
}
