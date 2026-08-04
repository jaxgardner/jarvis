import SwiftUI

@main
struct JarvisApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var api = JarvisAPI.shared
    @StateObject private var toasts = ToastCenter()

    var body: some Scene {
        WindowGroup {
            Group {
                if !api.isEnrolled {
                    SetupView()
                } else if api.isUnauthorized {
                    UnauthorizedView()
                } else {
                    RootView()
                }
            }
            .environmentObject(api)
            .environmentObject(toasts)
            // The design is one design, not a light one and a dark one. An
            // instrument panel that repaints itself white at 8am is a different
            // instrument.
            .preferredColorScheme(.dark)
            .tint(Theme.accent)
        }
    }
}

/// Which tab is showing. A custom bar rather than `TabView` because the pill
/// treatment, the mono labels and the pending-proposal badge are all part of
/// the design language, and the stock bar can carry none of them.
enum JarvisTab: String, CaseIterable, Hashable {
    case talk, agenda, pantry, gratitude, reports, proposals, health

    var label: String {
        switch self {
        case .talk: return "Talk"
        case .agenda: return "Agenda"
        case .pantry: return "Pantry"
        case .gratitude: return "Gratitude"
        case .reports: return "Reports"
        case .proposals: return "Review"
        case .health: return "Health"
        }
    }

    /// The brief's SF Symbols, kept as-is where it named them.
    ///
    /// Reports is the exception: `gearshape.2` was drawn for the tab when it
    /// was called Jobs and said "background machinery". What lands here is
    /// written work you asked for, so it gets a document.
    var symbol: String {
        switch self {
        case .talk: return "mic.fill"
        case .agenda: return "calendar"
        case .pantry: return "refrigerator"
        case .gratitude: return "sparkles"
        case .reports: return "doc.text.magnifyingglass"
        case .proposals: return "tray.full"
        case .health: return "heart.text.square"
        }
    }
}

/// Talk is first and selected by default. The dashboard matters, but it is
/// not what the app is for — the reason this exists is to say something
/// and have it captured, and burying that behind a tab you have to choose
/// would make the app slower than the Shortcut it replaced.
struct RootView: View {
    @EnvironmentObject private var api: JarvisAPI
    @EnvironmentObject private var toasts: ToastCenter
    @ObservedObject private var router = LaunchRouter.shared

    @State private var tab: JarvisTab = .talk
    /// Kept at the root so the tab badge is current wherever you are — an
    /// unreviewed proposal is the one thing in the app with a deadline.
    @State private var pendingProposals = 0

    var body: some View {
        VStack(spacing: 0) {
            statusStrip

            ZStack {
                switch tab {
                case .talk: TalkView()
                case .agenda: AgendaView()
                case .pantry: PantryView()
                case .gratitude: GratitudeView()
                case .reports: ReportsView()
                case .proposals: ProposalsView(pendingCount: $pendingProposals)
                case .health: HealthView()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)

            tabBar
        }
        .jarvisBackground()
        .overlay(alignment: .bottom) {
            if let message = toasts.message {
                Toast(text: message)
                    .padding(.bottom, 86)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .task { await refreshBadge() }
        // A "Job finished" notification should land on the report, not on
        // whichever tab happened to be showing.
        .onChange(of: router.pendingJobID) { _, id in
            if id != nil { tab = .reports }
        }
        // The mic latch can be set while another tab is showing — a tapped
        // gratitude push, from anywhere in the app. TalkView consumes it, but
        // only once it is on screen. Cold launch needs no equivalent: Talk is
        // already the default tab.
        .onChange(of: router.shouldStartListening) { _, wants in
            if wants { tab = .talk }
        }
    }

    private var statusStrip: some View {
        HStack {
            Text("JARVIS")
                .font(Theme.mono(10.5))
                .tracking(2.0)
                .foregroundStyle(Theme.text3)
            Spacer()
            // Not decoration: this is the standing answer to "is the private
            // network up", which is what nearly every failure here turns out
            // to be.
            Text(api.isReachable ? "⌁" : "⚠")
                .font(Theme.mono(12))
                .foregroundStyle(api.isReachable ? Theme.accent : Theme.danger)
        }
        .padding(.horizontal, 22)
        .padding(.top, 4)
        .padding(.bottom, 2)
    }

    private var tabBar: some View {
        HStack(spacing: 2) {
            ForEach(JarvisTab.allCases, id: \.self) { item in
                let isOn = item == tab
                Button {
                    tab = item
                } label: {
                    VStack(spacing: 4) {
                        Image(systemName: item.symbol)
                            .font(.system(size: 15))
                            .overlay(alignment: .topTrailing) {
                                if item == .proposals && pendingProposals > 0 {
                                    Circle()
                                        .fill(Theme.warning)
                                        .frame(width: 6, height: 6)
                                        .offset(x: 6, y: -2)
                                }
                            }
                        Text(item.label)
                            .font(Theme.mono(9.5))
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 7)
                    .background(
                        isOn ? Theme.dim(Theme.accent) : .clear,
                        in: RoundedRectangle(cornerRadius: 12, style: .continuous)
                    )
                    .overlay {
                        if isOn {
                            RoundedRectangle(cornerRadius: 12, style: .continuous)
                                .strokeBorder(Theme.accent.opacity(0.25), lineWidth: 1)
                        }
                    }
                    .foregroundStyle(isOn ? Theme.accent : Theme.text3)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(item.label)
                .accessibilityAddTraits(isOn ? [.isButton, .isSelected] : .isButton)
            }
        }
        .padding(.horizontal, 6)
        .padding(.top, 6)
        .padding(.bottom, 2)
        .background(alignment: .top) {
            Theme.void.opacity(0.55)
                .overlay(alignment: .top) { Theme.border.frame(height: 1) }
                .ignoresSafeArea(edges: .bottom)
        }
        .animation(.easeOut(duration: 0.15), value: tab)
    }

    private func refreshBadge() async {
        pendingProposals = (try? await api.proposals().proposals.count) ?? 0
    }
}

/// The token was rejected. Nothing else in the app can work, so this takes the
/// whole screen rather than repeating itself on six tabs.
struct UnauthorizedView: View {
    @EnvironmentObject private var api: JarvisAPI

    var body: some View {
        VStack {
            ErrorState(
                title: "This device isn't authorized.",
                detail: "Re-enroll to get a new token.",
                hint: "The old one may have been revoked from another device — that is what revocation is for."
            )
            Button("Re-enroll") { api.forget() }
                .buttonStyle(FilledButtonStyle())
                .padding(.horizontal, 60)
                .padding(.top, 20)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .jarvisBackground()
    }
}
