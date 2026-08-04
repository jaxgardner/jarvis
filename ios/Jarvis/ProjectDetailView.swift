import SwiftUI

/// One project: the thinking, the research, the dates, the links, the files.
///
/// Thinking is first and everything else follows it, because the reason to
/// open this screen is almost always to reread what you were last thinking.
///
/// There is no status header. The server stores none, and a paragraph invented
/// here would be a second answer to "where am I on this" that could disagree
/// with the one Talk gives you — which is generated from these very rows.
///
/// Empty sections are omitted rather than rendered with a placeholder. A
/// project started five minutes ago would otherwise be five headings and
/// nothing else.
struct ProjectDetailView: View {
    @EnvironmentObject private var api: JarvisAPI
    @EnvironmentObject private var toasts: ToastCenter

    let projectId: Int
    let name: String

    @State private var detail: ProjectDetail?
    @State private var error: String?
    @State private var isLoading = false
    @State private var newLink = ""
    @State private var reading: Artifact?

    /// A file pulled for reading. Identifiable so `.sheet(item:)` drives it —
    /// `isPresented` plus a separate optional can disagree, and does.
    struct Artifact: Identifiable {
        var id: String { name }
        let name: String
        let text: String
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if let error {
                    ErrorBanner(message: error)
                }
                if let detail {
                    if let description = detail.description, !description.isEmpty {
                        Text(description)
                            .font(Theme.sans(14))
                            .foregroundStyle(Theme.text2)
                    }
                    thinking(detail)
                    reports(detail)
                    dates(detail)
                    links(detail)
                    files(detail)
                } else if error == nil {
                    EmptyState(title: "Loading", message: "Reading the project.")
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 14)
        }
        .jarvisBackground()
        .navigationTitle(name)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { statusMenu }
        .refreshable { await load() }
        .task { await load() }
        .sheet(item: $reading) { artifact in
            NavigationStack {
                ScrollView {
                    Text(artifact.text)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.text)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                        .padding(16)
                }
                .jarvisBackground()
                .navigationTitle(artifact.name)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Done") { reading = nil }
                    }
                }
            }
        }
    }

    // MARK: - Sections

    @ViewBuilder
    private func thinking(_ detail: ProjectDetail) -> some View {
        if !detail.notes.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                SectionLabel(text: "Thinking")
                ForEach(detail.notes) { note in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(note.body)
                            .font(Theme.sans(14.5))
                            .foregroundStyle(Theme.text)
                        Text(note.when)
                            .font(Theme.mono(10))
                            .foregroundStyle(Theme.text3)
                    }
                    .jarvisCard()
                }
            }
        }
    }

    @ViewBuilder
    private func reports(_ detail: ProjectDetail) -> some View {
        if !detail.reports.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                SectionLabel(text: "Reports")
                ForEach(detail.reports) { report in
                    VStack(alignment: .leading, spacing: 8) {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(report.prompt)
                                .font(Theme.sans(14.5, weight: .medium))
                                .foregroundStyle(Theme.text)
                            Spacer(minLength: 4)
                            Pill(text: report.status.uppercased(), tint: tint(report.status))
                        }

                        // The summary, not the report. The full text is on the
                        // Reports screen behind Health — this is the glance.
                        Text(report.summary ?? report.error ?? "Still working.")
                            .font(Theme.sans(13))
                            .foregroundStyle(Theme.text2)

                        // The same box the Reports screen uses, so answering a
                        // report from here and from there are one code path.
                        ReplyBox(
                            jobID: report.id,
                            isLive: report.status == "queued" || report.status == "running",
                            onSent: { Task { await load() } }
                        )
                    }
                    .jarvisCard()
                }
            }
        }
    }

    @ViewBuilder
    private func dates(_ detail: ProjectDetail) -> some View {
        if !detail.events.isEmpty || !detail.reminders.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                SectionLabel(text: "Dates")
                ForEach(detail.events) { event in
                    row(
                        title: event.title,
                        detail: event.location.map { "\(event.when) · \($0)" } ?? event.when,
                        symbol: "calendar"
                    )
                }
                ForEach(detail.reminders) { reminder in
                    row(title: reminder.body, detail: reminder.when, symbol: "bell")
                }
            }
        }
    }

    @ViewBuilder
    private func links(_ detail: ProjectDetail) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionLabel(text: "Links")
            ForEach(detail.links) { link in
                if let url = URL(string: link.url) {
                    Link(destination: url) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(link.title ?? link.url)
                                .font(Theme.sans(14))
                                .foregroundStyle(Theme.accent)
                                .lineLimit(2)
                            if link.title != nil {
                                Text(link.url)
                                    .font(Theme.mono(10))
                                    .foregroundStyle(Theme.text3)
                                    .lineLimit(1)
                            }
                        }
                        .jarvisCard()
                    }
                }
            }

            // The one thing about a project that is typed rather than said:
            // you cannot speak a URL.
            HStack(spacing: 8) {
                TextField("Paste a link", text: $newLink)
                    .font(Theme.sans(13.5))
                    .foregroundStyle(Theme.text)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .submitLabel(.done)
                    .onSubmit { Task { await addLink() } }
                Button("Add") { Task { await addLink() } }
                    .font(Theme.sans(13.5, weight: .medium))
                    .disabled(newLink.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .jarvisCard()
        }
    }

    @ViewBuilder
    private func files(_ detail: ProjectDetail) -> some View {
        if !detail.files.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                SectionLabel(text: "Files")
                ForEach(detail.files) { file in
                    Button { Task { await read(file.name) } } label: {
                        row(
                            title: file.name,
                            detail: "\(file.bytes) bytes · \(file.when)",
                            symbol: "doc.text"
                        )
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private func row(title: String, detail: String, symbol: String) -> some View {
        HStack(spacing: 10) {
            Image(systemName: symbol)
                .font(.system(size: 13))
                .foregroundStyle(Theme.text3)
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(Theme.sans(14))
                    .foregroundStyle(Theme.text)
                Text(detail)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.text3)
            }
            Spacer(minLength: 0)
        }
        .jarvisCard()
    }

    private func tint(_ status: String) -> Color {
        switch status {
        case "done": return Theme.success
        case "failed": return Theme.danger
        default: return Theme.accent
        }
    }

    // MARK: - Status

    @ToolbarContentBuilder
    private var statusMenu: some ToolbarContent {
        ToolbarItem(placement: .topBarTrailing) {
            Menu {
                Button("Active") { Task { await setStatus("active") } }
                Button("Paused") { Task { await setStatus("paused") } }
                Button("Done") { Task { await setStatus("done") } }
            } label: {
                Image(systemName: "ellipsis.circle")
            }
        }
    }

    // MARK: - Actions

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            detail = try await api.project(projectId)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func addLink() async {
        let url = newLink.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !url.isEmpty else { return }
        do {
            detail = try await api.addLink(project: projectId, url: url, title: nil)
            newLink = ""
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func setStatus(_ status: String) async {
        do {
            detail = try await api.setStatus(project: projectId, status: status)
            // Marking a project done takes it out of the router's PROJECTS
            // block, so say so — otherwise the next thing you file by voice
            // quietly lands nowhere.
            toasts.show(status == "active" ? "Back in play." : "Marked \(status).")
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func read(_ name: String) async {
        do {
            reading = Artifact(
                name: name,
                text: try await api.projectFile(project: projectId, name: name)
            )
        } catch {
            self.error = error.localizedDescription
        }
    }
}
