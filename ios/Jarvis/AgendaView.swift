import SwiftUI

/// What's coming up.
///
/// Every time string here comes from the server's `when` field. The server
/// already renders "tomorrow at 3 PM" for the spoken replies; re-deriving it
/// from `starts_at` on the client would mean two implementations of relative
/// dates that drift apart, and the one you'd trust is the one you can't see.
struct AgendaView: View {
    @EnvironmentObject private var api: JarvisAPI

    @State private var agenda: AgendaResponse?
    @State private var days = 1
    @State private var error: String?
    @State private var isLoading = false

    var body: some View {
        NavigationStack {
            List {
                Picker("Window", selection: $days) {
                    Text("Today").tag(1)
                    Text("3 days").tag(3)
                    Text("Week").tag(7)
                }
                .pickerStyle(.segmented)
                .listRowSeparator(.hidden)

                if let error {
                    Text(error).foregroundStyle(.red).font(.footnote)
                }

                if let agenda {
                    if agenda.events.isEmpty && agenda.reminders.isEmpty {
                        ContentUnavailableView(
                            "Nothing scheduled",
                            systemImage: "calendar",
                            description: Text("Looks clear.")
                        )
                        .listRowSeparator(.hidden)
                    }

                    if !agenda.events.isEmpty {
                        Section("Events") {
                            ForEach(agenda.events) { event in
                                row(title: event.title, when: event.when, detail: event.location)
                            }
                        }
                    }

                    if !agenda.reminders.isEmpty {
                        Section("Reminders") {
                            ForEach(agenda.reminders) { reminder in
                                row(title: reminder.body, when: reminder.when, detail: nil)
                                    .swipeActions(edge: .trailing) {
                                        Button {
                                            Task { await act { try await api.ack(reminder: reminder.id) } }
                                        } label: {
                                            Label("Done", systemImage: "checkmark")
                                        }
                                        .tint(.green)

                                        Button {
                                            Task {
                                                await act {
                                                    try await api.snooze(reminder: reminder.id, minutes: 10)
                                                }
                                            }
                                        } label: {
                                            Label("Snooze", systemImage: "clock.arrow.circlepath")
                                        }
                                        .tint(.orange)
                                    }
                            }
                        }
                    }
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle("Agenda")
            .refreshable { await load() }
            .task(id: days) { await load() }
            .overlay { if isLoading && agenda == nil { ProgressView() } }
        }
    }

    private func row(title: String, when: String, detail: String?) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
            Text(detail.map { "\(when) · \($0)" } ?? when)
                .font(.caption)
                .foregroundStyle(.secondary)
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
    private func act(_ work: () async throws -> Void) async {
        do {
            try await work()
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
