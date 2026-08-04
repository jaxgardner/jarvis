import SwiftUI

/// Three things a day.
///
/// The empty slots are the point. This screen answers "have I done it?" at a
/// glance, which is what you open it for between the ten o'clock push and bed
/// — a list of what you already wrote would answer a question nobody is
/// asking at that hour.
///
/// Read-only by design: capture happens in Talk, out loud. A text field here
/// would be a second way to write the same rows, and the one in Talk already
/// works when you are lying down with the lights off.
struct GratitudeView: View {
    @EnvironmentObject private var api: JarvisAPI

    @State private var gratitude: GratitudeResponse?
    @State private var error: String?
    @State private var isLoading = false

    var body: some View {
        VStack(spacing: 0) {
            ScreenHeader(title: "Gratitude", kicker: "Three things a day")
            RefreshStamp(isLoading: isLoading, failed: error != nil)
            content
        }
        .task { await load() }
    }

    @ViewBuilder
    private var content: some View {
        if gratitude == nil, let error {
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
        } else if let gratitude {
            ScrollView {
                VStack(spacing: 14) {
                    if let error {
                        ErrorBanner(message: error)
                    }

                    todayCard(gratitude)

                    if gratitude.days.isEmpty {
                        EmptyState(
                            title: "No history yet",
                            message: "Say what you're grateful for in Talk and the days pile up here."
                        )
                        .padding(.top, 30)
                    }

                    ForEach(gratitude.days) { day in
                        dayCard(day)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.bottom, 24)
            }
            .refreshable { await load() }
        } else {
            Spacer()
            ProgressView().tint(Theme.accent)
            Spacer()
        }
    }

    private func todayCard(_ gratitude: GratitudeResponse) -> some View {
        let entries = gratitude.today.entries
        let target = gratitude.today.target

        return VStack(alignment: .leading, spacing: 0) {
            HStack {
                SectionLabel(text: "Today")
                Spacer()
                Pill(
                    text: "\(min(entries.count, target)) of \(target)",
                    tint: entries.count >= target ? Theme.success : Theme.accent
                )
            }

            if gratitude.streak > 0 {
                Text("\(gratitude.streak)-day streak")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.text3)
                    .padding(.top, 6)
            }

            VStack(alignment: .leading, spacing: 9) {
                // Slots, not rows: an empty one is as informative as a full
                // one. A fourth thing lands after the three and is welcome.
                ForEach(0..<max(target, entries.count), id: \.self) { index in
                    slot(number: index + 1, body: index < entries.count ? entries[index].body : nil)
                }
            }
            .padding(.top, 12)
        }
        .jarvisCard()
    }

    private func slot(number: Int, body: String?) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text("\(number)")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.text3)
                .frame(width: 12, alignment: .leading)
            if let body {
                Text(body)
                    .font(Theme.sans(15))
                    .foregroundStyle(Theme.text)
            } else {
                Text("·")
                    .font(Theme.sans(15))
                    .foregroundStyle(Theme.text3)
            }
            Spacer(minLength: 0)
        }
    }

    private func dayCard(_ day: GratitudeResponse.Day) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionLabel(text: Self.spoken(day.on))
            VStack(alignment: .leading, spacing: 6) {
                ForEach(day.entries) { entry in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("·")
                            .font(Theme.sans(14))
                            .foregroundStyle(Theme.text3)
                        Text(entry.body)
                            .font(Theme.sans(14.5))
                            .foregroundStyle(Theme.text2)
                        Spacer(minLength: 0)
                    }
                }
            }
            .padding(.top, 9)
        }
        .jarvisCard()
    }

    /// "Aug 3" — the year is noise on a screen you scroll rather than search.
    static func spoken(_ isoDay: String) -> String {
        let parser = DateFormatter()
        parser.dateFormat = "yyyy-MM-dd"
        parser.timeZone = TimeZone(identifier: "UTC")
        guard let date = parser.date(from: isoDay) else { return isoDay }

        let display = DateFormatter()
        display.dateFormat = "MMM d"
        display.timeZone = TimeZone(identifier: "UTC")
        return display.string(from: date)
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            gratitude = try await api.gratitude()
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}
