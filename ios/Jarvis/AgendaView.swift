import SwiftUI

/// What's coming up.
///
/// Every time string here comes from the server's `when` field. The server
/// already renders "tomorrow at 3 PM" for the spoken replies; re-deriving it
/// from `starts_at` on the client would mean two implementations of relative
/// dates that drift apart, and the one you'd trust is the one you can't see.
struct AgendaView: View {
    @EnvironmentObject private var api: JarvisAPI
    @EnvironmentObject private var toasts: ToastCenter

    @State private var agenda: AgendaResponse?
    @State private var days = 1
    @State private var error: String?
    @State private var isLoading = false

    var body: some View {
        VStack(spacing: 0) {
            ScreenHeader(title: "Agenda")

            SegmentedBar(
                options: [("Today", 1), ("3 days", 3), ("Week", 7)],
                selection: $days
            )
            .padding(.horizontal, 20)
            .padding(.bottom, 8)

            RefreshStamp(isLoading: isLoading, failed: error != nil)

            content
        }
        .task(id: days) { await load() }
    }

    @ViewBuilder
    private var content: some View {
        if agenda == nil, let error {
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
        } else if agenda == nil {
            Spacer()
            ProgressView().tint(Theme.accent)
            Spacer()
        } else if let agenda {
            List {
                if let error {
                    ErrorBanner(message: error).plainRow()
                }

                if agenda.events.isEmpty && agenda.reminders.isEmpty {
                    EmptyState(title: "Nothing scheduled", message: "Looks clear.")
                        .plainRow()
                }

                if !agenda.events.isEmpty {
                    Section {
                        ForEach(agenda.events) { event in
                            EventCard(event: event).plainRow()
                        }
                    } header: {
                        SectionLabel(text: "Events").sectionRow()
                    }
                }

                if !agenda.reminders.isEmpty {
                    Section {
                        ForEach(agenda.reminders) { reminder in
                            ReminderCard(reminder: reminder)
                                .plainRow()
                                .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                                    Button {
                                        Task {
                                            await act("Marked done") {
                                                try await api.ack(reminder: reminder.id)
                                            }
                                        }
                                    } label: {
                                        Label("Done", systemImage: "checkmark")
                                    }
                                    .tint(Theme.success)

                                    Button {
                                        Task {
                                            await act("Snoozed 10 minutes") {
                                                try await api.snooze(
                                                    reminder: reminder.id, minutes: 10
                                                )
                                            }
                                        }
                                    } label: {
                                        Label("Snooze", systemImage: "clock.arrow.circlepath")
                                    }
                                    .tint(Theme.warning)
                                }
                        }
                    } header: {
                        SectionLabel(text: "Reminders").sectionRow()
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
            agenda = try await api.agenda(days: days)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// Run a mutation, then reload — the server is the source of truth, so the
    /// list is refetched rather than patched locally.
    private func act(_ confirmation: String, _ work: () async throws -> Void) async {
        do {
            try await work()
            toasts.show(confirmation)
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct EventCard: View {
    let event: AgendaResponse.Event

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(event.title)
                .font(Theme.sans(15, weight: .semibold))
                .foregroundStyle(Theme.text)
            Text(event.location.map { "\(event.when) · \($0)" } ?? event.when)
                .font(Theme.sans(13))
                .foregroundStyle(Theme.text2)
        }
        .jarvisCard(padding: 12)
    }
}

private struct ReminderCard: View {
    let reminder: AgendaResponse.Reminder

    /// `missed` is not `fired`. It means the reminder came due while the
    /// machine was off and was too stale to deliver — nothing told you, which
    /// is exactly the case worth flagging.
    private var statusPill: (text: String, tint: Color)? {
        switch reminder.status {
        case "missed": return ("missed", Theme.warning)
        case "acked": return ("done ✓", Theme.success)
        case "fired": return ("fired", Theme.text2)
        case "firing": return ("firing", Theme.accent)
        case "cancelled": return ("cancelled", Theme.text3)
        default: return nil
        }
    }

    private var isSettled: Bool {
        reminder.status == "acked" || reminder.status == "cancelled"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(reminder.body)
                .font(Theme.sans(15, weight: .semibold))
                .strikethrough(isSettled, color: Theme.text3)
                .foregroundStyle(isSettled ? Theme.text3 : Theme.text)

            HStack(spacing: 6) {
                Text(reminder.when)
                    .font(Theme.sans(13))
                    .foregroundStyle(Theme.text2)
                if let statusPill {
                    Pill(text: statusPill.text, tint: statusPill.tint)
                }
            }
        }
        .jarvisCard(padding: 12)
    }
}

// MARK: - List plumbing

extension View {
    /// A card sitting directly on the app background — no separator, no
    /// system-grey row fill, no inset that fights the card's own padding.
    func plainRow() -> some View {
        self
            .listRowBackground(Color.clear)
            .listRowSeparator(.hidden)
            .listRowInsets(EdgeInsets(top: 4, leading: 20, bottom: 4, trailing: 20))
    }

    func sectionRow() -> some View {
        self
            .textCase(nil)
            .listRowInsets(EdgeInsets(top: 12, leading: 20, bottom: 4, trailing: 20))
            .listRowBackground(Color.clear)
    }
}
