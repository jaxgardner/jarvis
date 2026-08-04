import SwiftUI

/// The projects you have going, and a way into each one.
///
/// Active first, then whatever was touched most recently. Paused and done sit
/// under a label rather than on another screen — a finished project is
/// something you still look back at, and hiding it behind a filter makes the
/// list lie about how much you have done.
///
/// The cards carry counts, not a summary. The server stores no status and this
/// screen invents none: what a project amounts to is the rows in it, and the
/// prose answer lives in Talk, generated from those same rows at the moment
/// you ask.
struct ProjectsView: View {
    @EnvironmentObject private var api: JarvisAPI

    @State private var projects: [ProjectsResponse.Project] = []
    @State private var error: String?
    @State private var isLoading = false
    @State private var hasLoaded = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ScreenHeader(title: "Projects", kicker: "What you're working on")
                RefreshStamp(isLoading: isLoading, failed: error != nil)
                content
            }
            .jarvisBackground()
        }
        .task { await load() }
    }

    private var active: [ProjectsResponse.Project] {
        projects.filter { $0.status == "active" }
    }

    private var closed: [ProjectsResponse.Project] {
        projects.filter { $0.status != "active" }
    }

    @ViewBuilder
    private var content: some View {
        if projects.isEmpty, let error {
            ScrollView {
                ErrorState(
                    title: "Can't reach the Mini",
                    detail: Failure.reason(error),
                    hint: "Usually means the private network (Tailscale) is off.",
                    retry: { Task { await load() } }
                )
            }
            .refreshable { await load() }
        } else if projects.isEmpty, hasLoaded {
            ScrollView {
                EmptyState(
                    title: "Nothing started",
                    message: "Say “start a project on…” in Talk. Add a research ask to the same sentence and it begins looking straight away."
                )
            }
            .refreshable { await load() }
        } else {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    if let error {
                        ErrorBanner(message: error)
                    }

                    ForEach(active) { card($0) }

                    if !closed.isEmpty {
                        SectionLabel(text: "Finished and paused")
                            .padding(.top, 10)
                        ForEach(closed) { card($0) }
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 12)
            }
            .refreshable { await load() }
        }
    }

    private func card(_ project: ProjectsResponse.Project) -> some View {
        NavigationLink {
            ProjectDetailView(projectId: project.id, name: project.name)
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(project.name)
                        .font(Theme.sans(16, weight: .medium))
                        .foregroundStyle(Theme.text)
                    Spacer(minLength: 4)
                    if project.status != "active" {
                        Pill(text: project.status.uppercased(), tint: Theme.text3)
                    }
                }

                if let description = project.description, !description.isEmpty {
                    Text(description)
                        .font(Theme.sans(13))
                        .foregroundStyle(Theme.text2)
                }

                Text(counts(project))
                    .font(Theme.mono(10.5))
                    .foregroundStyle(Theme.text3)
            }
            .jarvisCard()
            .opacity(project.status == "active" ? 1 : 0.62)
        }
        .buttonStyle(.plain)
    }

    private func counts(_ project: ProjectsResponse.Project) -> String {
        var parts: [String] = []
        if project.noteCount > 0 { parts.append("\(project.noteCount) notes") }
        if project.reportCount > 0 { parts.append("\(project.reportCount) reports") }
        if project.linkCount > 0 { parts.append("\(project.linkCount) links") }
        return parts.isEmpty ? "nothing yet" : parts.joined(separator: " · ")
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false; hasLoaded = true }
        do {
            projects = try await api.projects().projects
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}
