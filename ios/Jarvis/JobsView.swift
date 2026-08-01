import SwiftUI

/// Deep-path history.
///
/// The list shows a truncated preview because job results are prose and most
/// of it goes unread; the full text is one tap away on the detail view. A
/// running job is a live thing — watching one finish is a real use case, so the
/// list polls while anything is in flight and says so with a pulsing dot rather
/// than a spinner that would imply the *list* is loading.
struct JobsView: View {
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
                ScreenHeader(title: "Jobs", kicker: hasLiveJob ? "Deep path · live" : "Deep path")
                content
            }
            .jarvisBackground()
            .navigationDestination(for: Int.self) { JobDetailView(jobID: $0) }
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
                        title: "No jobs yet",
                        message: "Anything Jarvis escalates to the deep path lands here."
                    )
                    .plainRow()
                }

                ForEach(jobs) { job in
                    Button {
                        path.append(job.id)
                    } label: {
                        JobCard(job: job)
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

private struct JobCard: View {
    let job: JobsResponse.Job

    /// A failed job's error *is* its result as far as the list is concerned —
    /// "what happened" is the question either way.
    private var preview: String {
        if let failure = job.error, !failure.isEmpty, job.status == "failed" { return failure }
        if let result = job.resultPreview, !result.isEmpty { return result }
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

struct JobDetailView: View {
    @EnvironmentObject private var api: JarvisAPI
    let jobID: Int

    @State private var job: JobDetail?
    @State private var error: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let error {
                    ErrorBanner(message: error)
                }

                if let job {
                    Pill(text: job.status, tint: JobStatus.tint(job.status))

                    section("Asked", body: job.prompt, tint: Theme.text)

                    if let result = job.result, !result.isEmpty {
                        // Often markdown-ish, sometimes a table. Shown as-is
                        // and selectable rather than rendered — a half-working
                        // markdown pass would mangle more than it fixed.
                        section("Result", body: result, tint: Theme.text2)
                    }
                    if let failure = job.error, !failure.isEmpty {
                        section("Error", body: failure, tint: Theme.danger, mono: true)
                    }
                    if job.sessionId != nil {
                        // Follow-ups resume this session, which is why "what
                        // about the second one" works across two utterances.
                        Text("Follow-ups resume this session.")
                            .font(Theme.sans(12))
                            .foregroundStyle(Theme.text3)
                            .padding(.top, 4)
                            .overlay(alignment: .top) {
                                Theme.border.frame(height: 1).offset(y: -8)
                            }
                    }
                } else if error == nil {
                    ProgressView().tint(Theme.accent)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(20)
        }
        .jarvisBackground()
        .navigationTitle("Job \(jobID)")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    private func section(
        _ title: String,
        body: String,
        tint: Color,
        mono: Bool = false
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title.uppercased())
                .font(Theme.mono(11))
                .tracking(0.6)
                .foregroundStyle(mono ? Theme.danger : Theme.text3)
            Text(body)
                .font(mono ? Theme.mono(13) : Theme.sans(14.5))
                .foregroundStyle(tint)
                .lineSpacing(3)
                .textSelection(.enabled)
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
