import SwiftUI

/// Deep-path history.
///
/// The list shows a truncated preview because job results are prose and most
/// of it goes unread; the full text is one tap away on the detail view. This
/// is also where a "Job finished" notification should eventually deep-link.
struct JobsView: View {
    @EnvironmentObject private var api: JarvisAPI

    @State private var jobs: [JobsResponse.Job] = []
    @State private var error: String?
    @State private var isLoading = false

    var body: some View {
        NavigationStack {
            List {
                if let error {
                    Text(error).foregroundStyle(.red).font(.footnote)
                }

                if jobs.isEmpty && !isLoading {
                    ContentUnavailableView(
                        "No jobs yet",
                        systemImage: "gearshape.2",
                        description: Text("Anything Jarvis escalates to the deep path lands here.")
                    )
                    .listRowSeparator(.hidden)
                }

                ForEach(jobs) { job in
                    NavigationLink {
                        JobDetailView(jobID: job.id)
                    } label: {
                        row(job)
                    }
                }
            }
            .listStyle(.plain)
            .navigationTitle("Jobs")
            .refreshable { await load() }
            .task { await load() }
            // Running jobs finish while you're looking at them.
            .task {
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(5))
                    if jobs.contains(where: { $0.status == "queued" || $0.status == "running" }) {
                        await load()
                    }
                }
            }
            .overlay { if isLoading && jobs.isEmpty { ProgressView() } }
        }
    }

    private func row(_ job: JobsResponse.Job) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(job.prompt)
                .font(.body)
                .lineLimit(2)

            if let preview = job.resultPreview, !preview.isEmpty {
                Text(preview)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }

            HStack(spacing: 6) {
                Tag(text: job.status, tint: Self.tint(for: job.status))
                if job.attempts > 1 {
                    Tag(text: "attempt \(job.attempts)", tint: .orange)
                }
            }
        }
        .padding(.vertical, 4)
    }

    static func tint(for status: String) -> Color {
        switch status {
        case "done": return .green
        case "failed": return .red
        case "running": return .blue
        default: return .secondary
        }
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

struct JobDetailView: View {
    @EnvironmentObject private var api: JarvisAPI
    let jobID: Int

    @State private var job: JobDetail?
    @State private var error: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let error {
                    Text(error).foregroundStyle(.red)
                }

                if let job {
                    Tag(text: job.status, tint: JobsView.tint(for: job.status))

                    section("Asked", body: job.prompt)

                    if let result = job.result, !result.isEmpty {
                        section("Result", body: result)
                    }
                    if let failure = job.error, !failure.isEmpty {
                        section("Error", body: failure)
                    }
                    if job.sessionId != nil {
                        // Follow-ups resume this session, which is why "what
                        // about the second one" works across two utterances.
                        Text("Follow-ups resume this session.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                } else if error == nil {
                    ProgressView()
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding()
        }
        .navigationTitle("Job \(jobID)")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    private func section(_ title: String, body: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Text(body)
                .textSelection(.enabled)
        }
    }

    private func load() async {
        do {
            job = try await api.job(jobID)
        } catch {
            self.error = error.localizedDescription
        }
    }
}
