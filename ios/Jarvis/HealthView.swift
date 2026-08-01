import SwiftUI

/// Is it alive, is it fast, is the sync running, and what is it costing.
///
/// p95 is shown against the 2s budget explicitly rather than as a bare number,
/// because a latency figure with no target next to it is just trivia — and
/// `CLAUDE.md` is unambiguous that p95 over 2s is a bug, not a data point.
///
/// This is also the door to the three surfaces that have no home of their own:
/// ingest health, the inbox, and enrolled devices. They live behind Health
/// rather than as tabs because you go looking for them when something feels
/// wrong, which is the same reason you open this screen at all.
struct HealthView: View {
    @EnvironmentObject private var api: JarvisAPI

    @State private var health: HealthResponse?
    @State private var metrics: MetricsResponse?
    @State private var days = 1
    @State private var error: String?
    @State private var isLoading = false

    private var staleSources: [String] { health?.ingest?.stale ?? [] }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ScreenHeader(title: "Health", kicker: "The Mini")
                RefreshStamp(isLoading: isLoading, failed: error != nil)

                ScrollView {
                    VStack(spacing: 14) {
                        if let error {
                            ErrorBanner(message: error)
                        }

                        // A silently stale agenda is the exact failure
                        // ingestion is built to catch, so it gets said out
                        // loud at the top rather than only inside a sub-screen
                        // nobody opens.
                        if !staleSources.isEmpty {
                            staleWarning
                        }

                        miniCard

                        SegmentedBar(
                            options: [("24h", 1), ("7 days", 7), ("30 days", 30)],
                            selection: $days
                        )

                        if let metrics {
                            latencyCard(metrics)
                            spendCard(metrics.spend)
                        }

                        navGroup
                    }
                    .padding(.horizontal, 20)
                    .padding(.bottom, 24)
                }
                .refreshable { await load() }
            }
            .jarvisBackground()
        }
        .task(id: days) { await load() }
    }

    // MARK: - Cards

    private var staleWarning: some View {
        NavigationLink {
            IngestHealthView()
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 13))
                VStack(alignment: .leading, spacing: 2) {
                    Text("\(staleSources.joined(separator: " and ")) hasn't synced in 6 hours")
                        .font(Theme.sans(13, weight: .semibold))
                    Text("The agenda may be missing things.")
                        .font(Theme.sans(12))
                        .foregroundStyle(Theme.warning.opacity(0.8))
                }
                Spacer()
                Text("›")
            }
            .foregroundStyle(Theme.warning)
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                Theme.dim(Theme.warning),
                in: RoundedRectangle(cornerRadius: 12, style: .continuous)
            )
        }
        .buttonStyle(.plain)
    }

    private var miniCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                SectionLabel(text: "Mini")
                Spacer()
                if let health {
                    Pill(
                        text: health.status,
                        tint: health.status == "ok" ? Theme.success : Theme.danger
                    )
                } else {
                    Pill(text: "unreachable", tint: Theme.danger)
                }
            }

            if let health {
                Text(
                    health.db.ok
                        ? "database · \(health.db.migrationsApplied ?? 0) migrations"
                        : "database · \(health.db.error ?? "error")"
                )
                .font(Theme.mono(11))
                .foregroundStyle(health.db.ok ? Theme.text2 : Theme.danger)
                .padding(.top, 8)

                VStack(alignment: .leading, spacing: 6) {
                    ForEach(health.configured.sorted(by: { $0.key < $1.key }), id: \.key) { item in
                        HStack(spacing: 8) {
                            Image(systemName: item.value ? "checkmark" : "circle")
                                .font(.system(size: 11, weight: .bold))
                                .foregroundStyle(item.value ? Theme.success : Theme.text3)
                                .frame(width: 12)
                            Text(Self.label(for: item.key))
                                .font(Theme.sans(13))
                                .foregroundStyle(Theme.text2)
                        }
                    }
                }
                .padding(.top, 10)
            }
        }
        .jarvisCard()
    }

    private func latencyCard(_ metrics: MetricsResponse) -> some View {
        let over = (metrics.fast.p95 ?? 0) > Theme.latencyBudgetMs

        return VStack(alignment: .leading, spacing: 0) {
            SectionLabel(text: "Latency vs. 2s budget")

            if over {
                Text("Over the 2s budget.")
                    .font(Theme.sans(12.5))
                    .foregroundStyle(Theme.danger)
                    .padding(.top, 8)
            }

            VStack(alignment: .leading, spacing: 10) {
                latencyRow("Fast path", metrics.fast, budgeted: true)
                latencyRow("Deep path", metrics.deep, budgeted: false)
            }
            .padding(.top, 10)
        }
        .jarvisCard()
    }

    private func latencyRow(
        _ title: String,
        _ value: MetricsResponse.Latency,
        budgeted: Bool
    ) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text("\(title) · \(value.count) calls")
                .font(Theme.sans(13, weight: .semibold))
                .foregroundStyle(Theme.text)

            if value.count > 0 {
                let over = budgeted && (value.p95 ?? 0) > Theme.latencyBudgetMs
                HStack(spacing: 6) {
                    Pill(text: "p50 \(value.p50 ?? 0) ms", emphasised: false)
                    Pill(
                        text: "p95 \(value.p95 ?? 0) ms",
                        tint: over ? Theme.danger : Theme.text2,
                        emphasised: over
                    )
                    Pill(text: "max \(value.max ?? 0) ms", emphasised: false)
                }
            } else {
                Text("No traffic in this window.")
                    .font(Theme.sans(12))
                    .foregroundStyle(Theme.text3)
            }
        }
    }

    private func spendCard(_ spend: MetricsResponse.Spend) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionLabel(text: "Spend")

            VStack(alignment: .leading, spacing: 4) {
                Text("\(spend.utterances) utterances · \(spend.modelCalls) model calls")
                Text("\(Self.grouped(spend.inputTokens)) in / \(Self.grouped(spend.outputTokens)) out tokens")
                Text("\(Self.usd(spend.usd, places: 3)) · \(Self.usd(spend.usdPerUtterance, places: 5)) / utterance")
                Text("≈ \(Self.usd(spend.usdPerMonthAtThisRate))/mo at this rate")
                    .foregroundStyle(Theme.text)
            }
            .font(Theme.mono(12))
            .foregroundStyle(Theme.text2)
            .padding(.top, 8)

            Text("Fast path only — the deep path runs on the Claude Code subscription, not API credits.")
                .font(Theme.sans(11))
                .foregroundStyle(Theme.text3)
                .padding(.top, 8)
                .overlay(alignment: .top) {
                    Theme.border.frame(height: 1).offset(y: -4)
                }
        }
        .jarvisCard()
    }

    private var navGroup: some View {
        VStack(spacing: 1) {
            NavRow(
                title: "Ingest health",
                badge: staleSources.isEmpty ? "ok" : "⚠ stale",
                badgeTint: staleSources.isEmpty ? Theme.text3 : Theme.warning
            ) {
                IngestHealthView()
            }
            NavRow(title: "Inbox") { InboxView() }
            NavRow(title: "Devices") { DevicesView() }
        }
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(Theme.border, lineWidth: 1)
        }
    }

    // MARK: - Formatting

    static func label(for key: String) -> String {
        switch key {
        case "anthropic_api_key": return "Anthropic key"
        case "jarvis_token": return "Shared token"
        case "ntfy_topic": return "ntfy"
        case "apns": return "APNs"
        case "google": return "Google"
        default: return key.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    static func usd(_ value: Double, places: Int = 2) -> String {
        String(format: "$%.\(places)f", value)
    }

    /// Thousands separators. Token counts are read as magnitudes, and "41200"
    /// takes a beat longer to size up than "41,200".
    static func grouped(_ value: Int) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        return formatter.string(from: NSNumber(value: value)) ?? "\(value)"
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            async let health = api.health()
            async let metrics = api.metrics(days: days)
            self.health = try await health
            self.metrics = try await metrics
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}
