import SwiftUI

/// Deep-path history.
///
/// Called Reports rather than Jobs because that is what they are to the person
/// reading them. "Job" is the server's word for a queue row and it belongs in
/// the schema; on a tab bar it names the machinery instead of the thing you
/// asked for. The API is untouched — this is `/jobs` all the way down.
///
/// The list shows a truncated preview because results are prose and most of it
/// goes unread; the full text is one tap away on the detail view. A running
/// report is a live thing — watching one finish is a real use case, so the list
/// polls while anything is in flight and says so with a pulsing dot rather than
/// a spinner that would imply the *list* is loading.
struct ReportsView: View {
    @EnvironmentObject private var api: JarvisAPI
    @ObservedObject private var router = LaunchRouter.shared

    @State private var jobs: [JobsResponse.Job]?
    @State private var error: String?
    @State private var isLoading = false
    @State private var path: [Int] = []

    private var hasLiveJob: Bool {
        jobs?.contains { $0.status == "queued" || $0.status == "running" } ?? false
    }

    var body: some View {
        NavigationStack(path: $path) {
            VStack(spacing: 0) {
                ScreenHeader(
                    title: "Reports",
                    kicker: hasLiveJob ? "Deep path · working" : "Deep path"
                )
                content
            }
            .jarvisBackground()
            .navigationDestination(for: Int.self) { ReportDetailView(jobID: $0) }
        }
        .task { await load() }
        // Running jobs finish while you're looking at them.
        .task {
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(5))
                if hasLiveJob { await load() }
            }
        }
        // A tapped "Job finished" notification lands on the job itself.
        .task { openPendingJob() }
        .onChange(of: router.pendingJobID) { _, _ in openPendingJob() }
    }

    @ViewBuilder
    private var content: some View {
        if jobs == nil, let error {
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
        } else if jobs == nil {
            Spacer()
            ProgressView().tint(Theme.accent)
            Spacer()
        } else if let jobs {
            List {
                if let error {
                    ErrorBanner(message: error).plainRow()
                }

                if jobs.isEmpty {
                    EmptyState(
                        title: "No reports yet",
                        message: "Anything Jarvis escalates to the deep path lands here."
                    )
                    .plainRow()
                }

                ForEach(jobs) { job in
                    Button {
                        path.append(job.id)
                    } label: {
                        ReportCard(job: job)
                    }
                    .buttonStyle(.plain)
                    .plainRow()
                }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .refreshable { await load() }
        }
    }

    private func openPendingJob() {
        guard let id = router.consumeJobRequest() else { return }
        path = [id]
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            jobs = try await api.jobs().jobs
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct ReportCard: View {
    let job: JobsResponse.Job

    /// A failed job's error *is* its result as far as the list is concerned —
    /// "what happened" is the question either way.
    ///
    /// Run through the markdown summariser rather than shown raw: a result that
    /// opens on a heading or a table would otherwise spend both of the card's
    /// two lines on `## Findings` and a row of pipes.
    private var preview: String {
        if let failure = job.error, !failure.isEmpty, job.status == "failed" { return failure }
        if let result = job.resultPreview, !result.isEmpty { return Markdown.summary(of: result) }
        return job.status == "running" ? "Working…" : "Queued."
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(job.prompt)
                .font(Theme.sans(14.5, weight: .semibold))
                .foregroundStyle(Theme.text)
                .lineLimit(2)

            Text(preview)
                .font(Theme.sans(13))
                .foregroundStyle(Theme.text2)
                .lineLimit(2)

            HStack(spacing: 6) {
                StatusDot(status: job.status)
                Pill(text: job.status, tint: JobStatus.tint(job.status))
                if job.attempts > 1 {
                    Pill(text: "attempt \(job.attempts)", tint: Theme.warning)
                }
            }
            .padding(.top, 2)
        }
        .jarvisCard()
    }
}

/// Pulses while the job is live. The only motion on the screen, so it reads as
/// "this one is still going" without a label.
private struct StatusDot: View {
    let status: String
    @State private var dim = false

    private var isLive: Bool { status == "running" || status == "queued" }

    var body: some View {
        Circle()
            .fill(JobStatus.tint(status))
            .frame(width: 6, height: 6)
            .opacity(isLive && dim ? 0.25 : 1)
            .animation(
                isLive
                    ? .easeInOut(duration: 0.9).repeatForever(autoreverses: true)
                    : .default,
                value: dim
            )
            .onAppear { dim = true }
            .accessibilityHidden(true)
    }
}

enum JobStatus {
    static func tint(_ status: String) -> Color {
        switch status {
        case "done": return Theme.success
        case "failed": return Theme.danger
        case "running": return Theme.accent
        default: return Theme.text2
        }
    }
}

struct ReportDetailView: View {
    @EnvironmentObject private var api: JarvisAPI
    @EnvironmentObject private var toasts: ToastCenter
    let jobID: Int

    @State private var job: JobDetail?
    @State private var error: String?
    /// The escape hatch for a result the parser mis-reads. Per-screen and not
    /// persisted: wanting to see the source of one report says nothing about
    /// the next one.
    @State private var showsRaw = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let error {
                    ErrorBanner(message: error)
                }

                if let job {
                    Pill(text: job.status, tint: JobStatus.tint(job.status))

                    section("Asked") {
                        Text(job.prompt)
                            .font(Theme.sans(14.5))
                            .foregroundStyle(Theme.text)
                            .lineSpacing(3)
                            .textSelection(.enabled)
                    }

                    if let result = job.result, !result.isEmpty {
                        section("Result", controls: { resultControls(for: result) }) {
                            if showsRaw {
                                Text(result)
                                    .font(Theme.mono(12.5))
                                    .foregroundStyle(Theme.text2)
                                    .lineSpacing(2)
                                    .textSelection(.enabled)
                            } else {
                                MarkdownText(result)
                            }
                        }
                    }

                    if let failure = job.error, !failure.isEmpty {
                        section("Error", tint: Theme.danger) {
                            Text(failure)
                                .font(Theme.mono(13))
                                .foregroundStyle(Theme.danger)
                                .lineSpacing(3)
                                .textSelection(.enabled)
                        }
                    }

                    ReplyBox(
                        jobID: jobID,
                        isLive: job.status == "queued" || job.status == "running",
                        onSent: { Task { await load() } }
                    )
                    .padding(.top, 4)
                    .overlay(alignment: .top) {
                        Theme.border.frame(height: 1).offset(y: -8)
                    }
                } else if error == nil {
                    ProgressView().tint(Theme.accent)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(20)
        }
        .jarvisBackground()
        .navigationTitle("Report \(jobID)")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        // A replied-to report reworks itself while you're looking at it. Same
        // 5s cadence as the list, and it idles as soon as the job settles.
        .task {
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(5))
                guard let status = job?.status, status == "queued" || status == "running"
                else { continue }
                await load()
            }
        }
    }

    /// Raw is a toggle rather than a setting, and Copy exists because a report
    /// is the one thing in this app you might want somewhere else.
    @ViewBuilder
    private func resultControls(for result: String) -> some View {
        SectionButton(
            symbol: showsRaw ? "textformat" : "chevron.left.forwardslash.chevron.right",
            label: showsRaw ? "Rendered" : "Raw"
        ) {
            showsRaw.toggle()
        }
        SectionButton(symbol: "doc.on.doc", label: "Copy") {
            UIPasteboard.general.string = result
            toasts.show("Copied.")
        }
    }

    private func section<Content: View, Controls: View>(
        _ title: String,
        tint: Color = Theme.text3,
        @ViewBuilder controls: () -> Controls = { EmptyView() },
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text(title.uppercased())
                    .font(Theme.mono(11))
                    .tracking(0.6)
                    .foregroundStyle(tint)
                Spacer(minLength: 8)
                controls()
            }
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func load() async {
        do {
            job = try await api.job(jobID)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}

/// The small control that sits on a section label. Deliberately quiet — these
/// are affordances you go looking for, not things the screen is offering.
private struct SectionButton: View {
    let symbol: String
    let label: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 11.5))
                .foregroundStyle(Theme.text3)
                .frame(width: 26, height: 22)
                .background(Theme.surface2, in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                .overlay {
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .strokeBorder(Theme.border, lineWidth: 1)
                }
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}
