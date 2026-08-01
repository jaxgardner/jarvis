import SwiftUI

/// The design system.
///
/// Jarvis is an instrument panel, not a consumer app: dark, terse, monospaced
/// where a number needs to be read precisely and proportional where a sentence
/// needs to be read quickly. Everything below is defined once here so a screen
/// can't quietly invent its own grey.
///
/// The palette is written in OKLCH — the same numbers the design uses — and
/// converted at runtime rather than pasted in as hex. Perceptual lightness is
/// the whole point of the ramp (0.15 → 0.19 → 0.235 → 0.29 reads as four even
/// steps; the equivalent hex codes do not look like anything), and a hex table
/// would drift the first time one value was nudged.
enum Theme {

    // MARK: - Surfaces

    /// Behind everything. Darker than `bg` so a sheet or a pushed screen still
    /// has somewhere to sit.
    static let void = Color(oklch: (0.07, 0.010, 250))
    static let bg = Color(oklch: (0.15, 0.012, 250))
    static let surface = Color(oklch: (0.19, 0.013, 250))
    static let surface2 = Color(oklch: (0.235, 0.015, 250))
    static let surface3 = Color(oklch: (0.29, 0.017, 250))
    /// The inset well behind a segmented control.
    static let well = Color(oklch: (0.11, 0.011, 250))

    static let border = Color.white.opacity(0.09)
    static let hairline = Color.white.opacity(0.07)

    // MARK: - Text

    static let text = Color(oklch: (0.97, 0.004, 250))
    static let text2 = Color(oklch: (0.72, 0.012, 250))
    static let text3 = Color(oklch: (0.52, 0.012, 250))
    /// Text on top of a filled accent button — near-black, not white.
    static let onAccent = Color(oklch: (0.12, 0.010, 250))

    // MARK: - Semantics

    /// The fast path, and every affirmative control.
    static let accent = Color(oklch: (0.72, 0.16, 231))
    /// The deep path. It is a different colour because it is a different
    /// thing — different latency, different expectations.
    static let deep = Color(oklch: (0.68, 0.15, 300))
    static let danger = Color(oklch: (0.65, 0.19, 25))
    static let success = Color(oklch: (0.72, 0.14, 150))
    static let warning = Color(oklch: (0.80, 0.13, 80))

    /// The wash behind a pill of the same colour.
    static func dim(_ color: Color) -> Color { color.opacity(0.16) }

    // MARK: - Type

    /// Numbers, identifiers, timestamps — anything you compare rather than
    /// read. Monospaced digits stop a latency figure from jittering as it
    /// updates.
    static func mono(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }

    static func sans(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight)
    }

    /// The 2s end-to-end budget from CLAUDE.md. Over it is a bug, so it is
    /// spelled out next to every number it governs.
    static let latencyBudgetMs = 2000
}

// MARK: - Backgrounds

extension View {
    /// The app's ground plane: two very dim corner glows over near-black, one
    /// accent and one deep, so the two paths are present even on an idle
    /// screen.
    func jarvisBackground() -> some View {
        background {
            ZStack {
                Theme.bg
                RadialGradient(
                    colors: [Theme.accent.opacity(0.10), .clear],
                    center: .init(x: 0.15, y: -0.05),
                    startRadius: 0,
                    endRadius: 460
                )
                RadialGradient(
                    colors: [Theme.deep.opacity(0.08), .clear],
                    center: .init(x: 0.9, y: 1.05),
                    startRadius: 0,
                    endRadius: 460
                )
            }
            .ignoresSafeArea()
        }
    }

    /// The raised card used for every list row and panel. One definition, so
    /// the corner radius and the shadow don't fork per screen.
    func jarvisCard(radius: CGFloat = 16, padding: CGFloat = 14) -> some View {
        self
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                LinearGradient(
                    colors: [Theme.surface2, Theme.surface],
                    startPoint: .top,
                    endPoint: .bottom
                ),
                in: RoundedRectangle(cornerRadius: radius, style: .continuous)
            )
            .overlay {
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .strokeBorder(Theme.hairline, lineWidth: 1)
            }
            .shadow(color: .black.opacity(0.45), radius: 10, x: 0, y: 6)
    }
}

// MARK: - Components

/// The one pill. Intent, latency, status, job id — the Activity screen leans on
/// these carrying meaning by colour alone, so they all come from here.
struct Pill: View {
    let text: String
    var tint: Color = Theme.text2
    /// `false` gives the neutral slate background used for facts that aren't
    /// good or bad news, like `p50 640 ms`.
    var emphasised: Bool = true

    var body: some View {
        Text(text)
            .font(Theme.mono(10))
            .padding(.horizontal, 7)
            .padding(.vertical, 2.5)
            .background(
                emphasised ? Theme.dim(tint) : Theme.surface3,
                in: RoundedRectangle(cornerRadius: 5, style: .continuous)
            )
            .foregroundStyle(tint)
    }
}

/// Screen title plus its mono kicker. Replaces `.navigationTitle` because the
/// stock large title can't carry the second line and doesn't match the density
/// of everything under it.
struct ScreenHeader<Trailing: View>: View {
    let title: String
    var kicker: String?
    @ViewBuilder var trailing: Trailing

    var body: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(Theme.sans(26, weight: .medium))
                    .tracking(-0.3)
                    .foregroundStyle(Theme.text)
                if let kicker {
                    Text(kicker.uppercased())
                        .font(Theme.mono(10.5))
                        .tracking(0.8)
                        .foregroundStyle(Theme.text3)
                }
            }
            Spacer(minLength: 8)
            trailing
        }
        .padding(.horizontal, 20)
        .padding(.top, 14)
        .padding(.bottom, 8)
    }
}

extension ScreenHeader where Trailing == EmptyView {
    init(title: String, kicker: String? = nil) {
        self.init(title: title, kicker: kicker, trailing: { EmptyView() })
    }
}

/// Small uppercase mono label above a group of rows.
struct SectionLabel: View {
    let text: String

    var body: some View {
        Text(text.uppercased())
            .font(Theme.mono(11))
            .tracking(0.6)
            .foregroundStyle(Theme.text3)
    }
}

/// The segmented control, styled as an inset well with an accent slug. Used for
/// the agenda window, the health window, and the pause length.
struct SegmentedBar<Tag: Hashable>: View {
    let options: [(label: String, tag: Tag)]
    @Binding var selection: Tag

    var body: some View {
        HStack(spacing: 4) {
            ForEach(options, id: \.tag) { option in
                let isOn = option.tag == selection
                Button {
                    withAnimation(.easeOut(duration: 0.15)) { selection = option.tag }
                } label: {
                    Text(option.label)
                        .font(Theme.sans(12.5, weight: isOn ? .semibold : .regular))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 7)
                        .background(
                            isOn ? Theme.accent : .clear,
                            in: RoundedRectangle(cornerRadius: 7, style: .continuous)
                        )
                        .foregroundStyle(isOn ? Theme.onAccent : Theme.text2)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(4)
        .background(Theme.well, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

/// Two lines, centred, no illustration. An empty agenda is information, not an
/// occasion.
struct EmptyState: View {
    let title: String
    let message: String

    var body: some View {
        VStack(spacing: 4) {
            Text(title)
                .font(Theme.sans(16))
                .foregroundStyle(Theme.text2)
            Text(message)
                .font(Theme.sans(13))
                .foregroundStyle(Theme.text3)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 60)
        .padding(.horizontal, 24)
    }
}

/// The error every other error is a special case of. In practice it means
/// Tailscale is off, and saying so is worth more than the reason string.
struct ErrorState: View {
    let title: String
    var detail: String?
    var hint: String?
    var retry: (() -> Void)?

    var body: some View {
        VStack(spacing: 14) {
            ZStack {
                Circle().fill(Theme.dim(Theme.danger)).frame(width: 44, height: 44)
                Text("!").font(Theme.sans(22, weight: .bold)).foregroundStyle(Theme.danger)
            }
            Text(title)
                .font(Theme.sans(17, weight: .semibold))
                .foregroundStyle(Theme.text)
            if let detail {
                Text(detail)
                    .font(Theme.sans(14))
                    .foregroundStyle(Theme.text3)
            }
            if let hint {
                Text(hint)
                    .font(Theme.sans(12.5))
                    .foregroundStyle(Theme.text3)
                    .frame(maxWidth: 260)
            }
            if let retry {
                Button("Retry", action: retry)
                    .buttonStyle(FilledButtonStyle())
                    // Narrower than the text it sits under. A full-bleed
                    // primary button reads as the point of the screen, and the
                    // point of this screen is the sentence above it.
                    .frame(maxWidth: 180)
                    .padding(.top, 4)
            }
        }
        .multilineTextAlignment(.center)
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 32)
    }
}

/// Inline failure banner for a screen that already has content on it.
struct ErrorBanner: View {
    let message: String

    var body: some View {
        Text(message)
            .font(Theme.sans(12.5))
            .foregroundStyle(Theme.danger)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(
                Theme.dim(Theme.danger),
                in: RoundedRectangle(cornerRadius: 10, style: .continuous)
            )
    }
}

struct FilledButtonStyle: ButtonStyle {
    var tint: Color = Theme.accent

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Theme.sans(15, weight: .semibold))
            .frame(maxWidth: .infinity)
            .padding(.vertical, 13)
            .background(tint, in: RoundedRectangle(cornerRadius: 11, style: .continuous))
            .foregroundStyle(Theme.onAccent)
            .opacity(configuration.isPressed ? 0.75 : 1)
    }
}

struct OutlineButtonStyle: ButtonStyle {
    var tint: Color = Theme.text2

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Theme.sans(15, weight: .medium))
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
            .background(
                configuration.isPressed ? Theme.surface3 : .clear,
                in: RoundedRectangle(cornerRadius: 11, style: .continuous)
            )
            .overlay {
                RoundedRectangle(cornerRadius: 11, style: .continuous)
                    .strokeBorder(Theme.border, lineWidth: 1)
            }
            .foregroundStyle(tint)
    }
}

/// A disclosure row in a grouped stack — Health's route to its sub-screens.
struct NavRow<Destination: View>: View {
    let title: String
    var badge: String?
    var badgeTint: Color = Theme.text3
    @ViewBuilder var destination: Destination

    var body: some View {
        NavigationLink {
            destination
        } label: {
            HStack(spacing: 8) {
                Text(title)
                    .font(Theme.sans(14))
                    .foregroundStyle(Theme.text)
                Spacer()
                if let badge {
                    Text(badge)
                        .font(Theme.mono(10))
                        .foregroundStyle(badgeTint)
                }
                Text("›")
                    .font(Theme.sans(15))
                    .foregroundStyle(Theme.text3)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 13)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .background(Theme.surface)
    }
}

/// The pull-to-refresh receipt. `refreshable` gives you the gesture but no
/// confirmation that anything happened, and on a screen that is mostly cached
/// numbers that ambiguity matters.
struct RefreshStamp: View {
    let isLoading: Bool
    /// The last attempt failed. Saying "updated just now" over stale data is
    /// worse than saying nothing — this screen's whole job is to be trusted.
    var failed: Bool = false

    private var label: String {
        if isLoading { return "Refreshing…" }
        return failed ? "↻ Couldn't refresh" : "↻ Updated just now"
    }

    var body: some View {
        Text(label)
            .font(Theme.mono(11))
            .foregroundStyle(failed && !isLoading ? Theme.danger : Theme.text3)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 20)
            .padding(.bottom, 8)
    }
}

/// Transient confirmation of a write. Deliberately not a dialog: accepting a
/// proposal or snoozing a reminder should cost one tap and no acknowledgement.
struct Toast: View {
    let text: String

    var body: some View {
        Text(text)
            .font(Theme.sans(13))
            .foregroundStyle(Theme.text)
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .background(Theme.surface3, in: Capsule())
            .overlay(Capsule().strokeBorder(Theme.border, lineWidth: 1))
            .shadow(color: .black.opacity(0.5), radius: 12, y: 4)
    }
}

/// One toast at a time, owned by the root so any screen can raise one.
@MainActor
final class ToastCenter: ObservableObject {
    @Published private(set) var message: String?
    private var token = 0

    func show(_ text: String) {
        token += 1
        let mine = token
        withAnimation(.easeOut(duration: 0.2)) { message = text }
        Task {
            try? await Task.sleep(for: .seconds(1.8))
            guard mine == token else { return }
            withAnimation(.easeIn(duration: 0.2)) { message = nil }
        }
    }
}

// MARK: - OKLCH

extension Color {
    /// Build a colour from OKLCH — perceptual lightness, chroma, hue in
    /// degrees. Out-of-gamut values clamp per channel, which is what every
    /// browser does with the same input.
    init(oklch: (l: Double, c: Double, h: Double)) {
        let hue = oklch.h * .pi / 180
        let a = oklch.c * cos(hue)
        let b = oklch.c * sin(hue)
        let l = oklch.l

        // OKLab → LMS (cube roots), then cube back to cone response.
        let lp = l + 0.3963377774 * a + 0.2158037573 * b
        let mp = l - 0.1055613458 * a - 0.0638541728 * b
        let sp = l - 0.0894841775 * a - 1.2914855480 * b
        let lc = lp * lp * lp
        let mc = mp * mp * mp
        let sc = sp * sp * sp

        // LMS → linear sRGB.
        let r = 4.0767416621 * lc - 3.3077115913 * mc + 0.2309699292 * sc
        let g = -1.2684380046 * lc + 2.6097574011 * mc - 0.3413193965 * sc
        let bl = -0.0041960863 * lc - 0.7034186147 * mc + 1.7076147010 * sc

        func encode(_ value: Double) -> Double {
            let clamped = min(max(value, 0), 1)
            return clamped <= 0.0031308
                ? 12.92 * clamped
                : 1.055 * pow(clamped, 1 / 2.4) - 0.055
        }

        self.init(red: encode(r), green: encode(g), blue: encode(bl))
    }
}
