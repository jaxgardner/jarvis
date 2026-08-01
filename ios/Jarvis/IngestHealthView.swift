import SwiftUI

/// Is the sync actually running?
///
/// The whole screen turns on one thing: `last_run_at` and `last_ok_at` are
/// separate, and collapsing them into a single green dot destroys the only
/// information here worth having.
///
/// - equal and recent → healthy
/// - a gap between them → it is running and failing
/// - both old → it isn't running at all
///
/// Those are three different fixes. A quietly stale agenda is the exact failure
/// ingestion exists to catch, and it announces itself nowhere else.
struct IngestHealthView: View {
    @EnvironmentObject private var api: JarvisAPI

    @State private var sources: [HealthResponse.IngestSource]?
    @State private var error: String?

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                if let error {
                    ErrorBanner(message: error)
                }

                if let sources {
                    if sources.isEmpty {
                        EmptyState(
                            title: "Nothing syncing",
                            message: "Neither Calendar nor Gmail has ever run on this server."
                        )
                    }
                    ForEach(sources) { source in
                        SourceCard(source: source)
                    }
                } else if error == nil {
                    ProgressView().tint(Theme.accent).padding(.top, 60)
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 16)
        }
        .jarvisBackground()
        .navigationTitle("Ingest health")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        do {
            sources = try await api.health().ingest?.sources ?? []
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct SourceCard: View {
    let source: HealthResponse.IngestSource

    private var verdict: (text: String, tint: Color, explain: String) {
        switch source.condition {
        case .healthy:
            return ("healthy", Theme.success, "Running, and the last run succeeded.")
        case .runningAndFailing:
            return (
                "⚠ running and failing",
                Theme.warning,
                "It is still running on schedule, but nothing has succeeded in over six hours. Check the credentials and the detail below."
            )
        case .notRunning:
            return (
                "⚠ not running",
                Theme.danger,
                "Nothing has run recently. The importer is probably not being launched at all."
            )
        case .neverRun:
            return (
                "never run",
                Theme.text3,
                "This source has never synced on this server."
            )
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(source.source.capitalized)
                    .font(Theme.sans(14.5, weight: .semibold))
                    .foregroundStyle(Theme.text)
                Spacer()
                Pill(text: verdict.text, tint: verdict.tint)
            }

            VStack(alignment: .leading, spacing: 3) {
                stamp("last run", source.lastRunAt)
                stamp("last ok", source.lastOkAt)
            }
            .padding(.top, 10)

            Text(verdict.explain)
                .font(Theme.sans(12.5))
                .foregroundStyle(Theme.text3)
                .padding(.top, 10)

            if let detail = source.detail, !detail.isEmpty {
                Text(detail)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.text3)
                    .padding(.top, 8)
                    .textSelection(.enabled)
            }
        }
        .jarvisCard()
    }

    private func stamp(_ label: String, _ value: String?) -> some View {
        HStack(spacing: 6) {
            Text("\(label):")
                .foregroundStyle(Theme.text3)
            Text(value.map(RelativeStamp.render) ?? "never")
                .foregroundStyle(Theme.text2)
        }
        .font(Theme.mono(12))
    }
}

/// "4 minutes ago", from an ISO 8601 timestamp with offset.
///
/// This is the one place the client formats a date, and it is a deliberate
/// exception rather than an oversight. The rule exists so the agenda's
/// "tomorrow at 3 PM" can't drift from the sentence Jarvis speaks — but there
/// is no spoken version of "last ok 4 minutes ago" to drift from, and the
/// server returns raw timestamps here.
enum RelativeStamp {
    private static let parser: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static let plainParser: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    private static let relative: RelativeDateTimeFormatter = {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .full
        return formatter
    }()

    /// "2h ago". For rows where the stamp sits beside other text and the full
    /// form would wrap.
    private static let brief: RelativeDateTimeFormatter = {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter
    }()

    static func date(from value: String) -> Date? {
        parser.date(from: value) ?? plainParser.date(from: value)
    }

    static func render(_ value: String) -> String {
        guard let date = date(from: value) else { return value }
        return relative.localizedString(for: date, relativeTo: Date())
    }

    static func short(_ value: String) -> String {
        guard let date = date(from: value) else { return value }
        return brief.localizedString(for: date, relativeTo: Date())
    }
}
