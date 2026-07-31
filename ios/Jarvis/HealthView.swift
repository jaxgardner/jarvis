import SwiftUI

/// Is it alive, is it fast, and what is it costing.
///
/// p95 is shown against the 2s budget explicitly rather than as a bare number,
/// because a latency figure with no target next to it is just trivia — and
/// `CLAUDE.md` is unambiguous that p95 over 2s is a bug, not a data point.
struct HealthView: View {
    @EnvironmentObject private var api: JarvisAPI

    @State private var health: HealthResponse?
    @State private var metrics: MetricsResponse?
    @State private var days = 1
    @State private var error: String?

    private static let budgetMs = 2000

    var body: some View {
        NavigationStack {
            List {
                if let error {
                    Section { Text(error).foregroundStyle(.red).font(.footnote) }
                }

                Section("Mini") {
                    if let health {
                        LabeledContent("Status") {
                            Tag(text: health.status, tint: health.status == "ok" ? .green : .red)
                        }
                        LabeledContent("Database") {
                            Tag(
                                text: health.db.ok
                                    ? "\(health.db.migrationsApplied ?? 0) migrations"
                                    : "error",
                                tint: health.db.ok ? .green : .red
                            )
                        }
                        ForEach(health.configured.sorted(by: { $0.key < $1.key }), id: \.key) { item in
                            LabeledContent(Self.label(for: item.key)) {
                                Image(systemName: item.value ? "checkmark.circle.fill" : "circle")
                                    .foregroundStyle(item.value ? .green : .secondary)
                            }
                        }
                    } else {
                        LabeledContent("Status") { Text("unreachable").foregroundStyle(.red) }
                    }
                }

                Picker("Window", selection: $days) {
                    Text("24h").tag(1)
                    Text("7 days").tag(7)
                    Text("30 days").tag(30)
                }
                .pickerStyle(.segmented)
                .listRowSeparator(.hidden)

                if let metrics {
                    Section("Latency") {
                        latency("Fast path", metrics.fast, budgeted: true)
                        latency("Deep path", metrics.deep, budgeted: false)
                    }

                    Section {
                        LabeledContent("Utterances", value: "\(metrics.spend.utterances)")
                        LabeledContent("Model calls", value: "\(metrics.spend.modelCalls)")
                        LabeledContent(
                            "Tokens",
                            value: "\(metrics.spend.inputTokens) in · \(metrics.spend.outputTokens) out"
                        )
                        LabeledContent("Cost", value: Self.usd(metrics.spend.usd))
                        LabeledContent(
                            "Per utterance",
                            value: Self.usd(metrics.spend.usdPerUtterance, places: 4)
                        )
                        LabeledContent("At this rate") {
                            Text("\(Self.usd(metrics.spend.usdPerMonthAtThisRate))/mo")
                                .foregroundStyle(.secondary)
                        }
                    } header: {
                        Text("Spend")
                    } footer: {
                        Text("Fast path only — the deep path runs on the Claude Code subscription, not API credits.")
                    }
                }
            }
            .navigationTitle("Health")
            .refreshable { await load() }
            .task(id: days) { await load() }
        }
    }

    private func latency(_ title: String, _ value: MetricsResponse.Latency, budgeted: Bool) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(title)
                Spacer()
                Text("\(value.count) calls").font(.caption).foregroundStyle(.secondary)
            }
            if value.count > 0 {
                HStack(spacing: 6) {
                    Tag(text: "p50 \(value.p50 ?? 0) ms")
                    Tag(
                        text: "p95 \(value.p95 ?? 0) ms",
                        tint: budgeted && (value.p95 ?? 0) > Self.budgetMs ? .red : .secondary
                    )
                    Tag(text: "max \(value.max ?? 0) ms")
                }
                if budgeted && (value.p95 ?? 0) > Self.budgetMs {
                    Text("Over the 2s budget.")
                        .font(.caption)
                        .foregroundStyle(.red)
                }
            } else {
                Text("No traffic in this window.").font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }

    static func label(for key: String) -> String {
        switch key {
        case "anthropic_api_key": return "Anthropic key"
        case "jarvis_token": return "Shared token"
        case "ntfy_topic": return "ntfy"
        case "apns": return "APNs"
        default: return key
        }
    }

    static func usd(_ value: Double, places: Int = 2) -> String {
        String(format: "$%.\(places)f", value)
    }

    private func load() async {
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
