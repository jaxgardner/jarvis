import SwiftUI

/// The one control on the Talk screen.
///
/// What it replaced was a blue circle with a microphone in it, which looked
/// like a voice memo app and said nothing about what Jarvis was doing. This is
/// built to the same brief as the rest of the app — an instrument, not a
/// consumer control: a ring of ticks with a sweep running round it, a glass
/// core, and nested rings where the glyph used to be.
///
/// There is no microphone symbol at all, deliberately. It named the hardware,
/// which is the one thing about this control nobody needs told; the rings name
/// the state instead.
///
/// Phase is still carried by colour, as it was, but now also by motion, which
/// is legible from further away and mid-sentence. Idle is still: nothing
/// travels and the core is dark. Listening is quick and cyan. Sending is slow
/// and violet, the deep path's colour, because that is the one phase where the
/// wait is somebody else's.
struct MicOrb: View {
    enum Phase {
        case idle, listening, sending

        var isActive: Bool { self != .idle }
    }

    let phase: Phase

    private static let frame: CGFloat = 118
    private static let core: CGFloat = 84
    private static let tickRadius: CGFloat = 54

    @State private var breathing = false

    private var tint: Color {
        switch phase {
        case .idle: return Theme.accent
        case .listening: return Theme.accent
        case .sending: return Theme.deep
        }
    }

    /// Seconds per revolution. Listening is the urgent one; sending is a
    /// machine working, not a person waiting.
    private var sweepPeriod: Double {
        phase == .listening ? 1.6 : 3.2
    }

    var body: some View {
        ZStack {
            bloom
            ticks
            if phase.isActive { sweep }
            core
            rings
        }
        .frame(width: Self.frame, height: Self.frame)
        .onAppear { breathing = true }
        .accessibilityHidden(true)
    }

    // MARK: - Layers

    /// The glow the orb sits in. Present but nearly out when idle — the screen
    /// should look asleep, not switched off.
    private var bloom: some View {
        Circle()
            .fill(
                RadialGradient(
                    colors: [tint.opacity(0.40), tint.opacity(0.06), .clear],
                    center: .center,
                    startRadius: 8,
                    endRadius: 76
                )
            )
            .frame(width: 152, height: 152)
            .blur(radius: 10)
            .opacity(phase.isActive ? 1 : 0.30)
            .scaleEffect(breathing ? 1.06 : 0.92)
            .animation(
                .easeInOut(duration: phase.isActive ? 1.4 : 3.0).repeatForever(autoreverses: true),
                value: breathing
            )
            .animation(.easeOut(duration: 0.35), value: phase)
    }

    private var ticks: some View {
        TickRing(radius: Self.tickRadius)
            .foregroundStyle(Theme.text3.opacity(phase.isActive ? 0.55 : 0.30))
            .animation(.easeOut(duration: 0.35), value: phase)
    }

    /// The sweep. A comet head with a short tail, masked over the same ticks so
    /// it lights them rather than drawing over them.
    private var sweep: some View {
        TickRing(radius: Self.tickRadius)
            .foregroundStyle(tint)
            .mask {
                Sweep(period: sweepPeriod)
            }
            .id(sweepPeriod)
            .shadow(color: tint.opacity(0.6), radius: 5)
            .transition(.opacity)
    }

    private var core: some View {
        Circle()
            .fill(
                RadialGradient(
                    colors: [Theme.surface3, Theme.void],
                    center: .init(x: 0.34, y: 0.26),
                    startRadius: 2,
                    endRadius: 92
                )
            )
            .overlay {
                // The tint bleeding up through the glass. This is most of what
                // separates a live orb from a dead one at arm's length.
                Circle().fill(
                    RadialGradient(
                        colors: [tint.opacity(phase.isActive ? 0.28 : 0.05), .clear],
                        center: .init(x: 0.5, y: 0.62),
                        startRadius: 0,
                        endRadius: 62
                    )
                )
            }
            .overlay { rim }
            .frame(width: Self.core, height: Self.core)
            .animation(.easeOut(duration: 0.35), value: phase)
    }

    /// A conic gradient round the edge, turning only while something is
    /// happening. Idle gets the same gradient held still, so the orb still has
    /// a lit edge without pretending to be busy.
    private var rim: some View {
        Circle()
            .strokeBorder(
                AngularGradient(
                    colors: [
                        tint.opacity(0.10),
                        tint,
                        tint.opacity(0.25),
                        tint.opacity(0.75),
                        tint.opacity(0.10),
                    ],
                    center: .center
                ),
                lineWidth: 1.5
            )
            .overlay {
                Circle().strokeBorder(Color.white.opacity(0.10), lineWidth: 1)
            }
            .modifier(SlowTurn(active: phase.isActive))
    }

    /// What sits where the microphone used to.
    ///
    /// A mic glyph names the hardware, which is the least interesting thing
    /// about this control and the one thing you already know. Rings name what
    /// is actually happening: they sit still when nothing is, travel outward
    /// while the mic is open, and fall inward while the server has your words.
    /// It is also the only part of the orb legible without looking directly at
    /// it, which is the case the Action Button creates.
    private var rings: some View {
        ZStack {
            Circle()
                .fill(tint.opacity(phase.isActive ? 0.95 : 0.5))
                .frame(width: 6, height: 6)
                .shadow(color: tint.opacity(phase.isActive ? 0.9 : 0), radius: 6)

            // Three, not more. The nest is a backdrop for the travelling ring,
            // and at five or six the two stop being distinguishable — you get
            // a texture of concentric circles rather than something crossing
            // them.
            ForEach(Array(Self.ringDiameters.enumerated()), id: \.offset) { index, diameter in
                Circle()
                    .strokeBorder(
                        tint.opacity(Self.ringOpacity[index] * (phase.isActive ? 0.8 : 1)),
                        lineWidth: index == 0 ? 1.6 : 1.1
                    )
                    .frame(width: diameter, height: diameter)
            }

            // And two pings, for the same reason: the gap between them is what
            // makes each one read as one thing moving.
            if phase.isActive {
                ForEach(0..<2, id: \.self) { index in
                    Ping(
                        tint: tint,
                        period: pingPeriod,
                        delay: Double(index) * pingPeriod / 2,
                        inward: phase == .sending
                    )
                }
                .id(phase)
                .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.35), value: phase)
    }

    private static let ringDiameters: [CGFloat] = [20, 38, 56]
    /// Held down while something is travelling, so the bright ring is the one
    /// that is moving.
    private static let ringOpacity: [Double] = [0.62, 0.38, 0.22]

    /// Listening emits at the pace of speech. Sending is slower, because it is
    /// a wait rather than a rhythm.
    private var pingPeriod: Double { phase == .listening ? 1.8 : 2.6 }
}

/// One travelling ring. Outward while listening, inward while sending — the
/// direction is the whole difference between "I'm hearing you" and "I'm working
/// on it", and it needs no colour to read.
private struct Ping: View {
    let tint: Color
    let period: Double
    let delay: Double
    let inward: Bool

    @State private var travelling = false

    /// Scale, not frame: only one of the two animates on the render thread.
    private var scale: CGFloat {
        let near: CGFloat = 0.18
        if inward { return travelling ? near : 1 }
        return travelling ? 1 : near
    }

    var body: some View {
        Circle()
            .strokeBorder(tint.opacity(travelling ? 0 : 0.85), lineWidth: 1.5)
            .frame(width: 62, height: 62)
            .scaleEffect(scale)
            .animation(
                .easeOut(duration: period).repeatForever(autoreverses: false).delay(delay),
                value: travelling
            )
            .onAppear { travelling = true }
    }
}

/// The dial the sweep runs round. Ticks rather than a solid ring because the
/// rest of the app measures things, and because a sweep reads as *travelling*
/// over discrete marks in a way it does not over a smooth arc.
private struct TickRing: View {
    var count: Int = 44
    let radius: CGFloat
    var length: CGFloat = 7

    var body: some View {
        ZStack {
            ForEach(0..<count, id: \.self) { index in
                Capsule()
                    .frame(width: 1.5, height: length)
                    .offset(y: -radius)
                    .rotationEffect(.degrees(Double(index) / Double(count) * 360))
            }
        }
    }
}

/// The rotating mask: opaque at the head, transparent a third of the way
/// behind it. Owns its own `turning` latch so the caller can restart it with
/// `.id(period)` rather than having to drive an angle.
private struct Sweep: View {
    let period: Double

    @State private var turning = false

    var body: some View {
        AngularGradient(
            gradient: Gradient(stops: [
                .init(color: .white, location: 0.00),
                .init(color: .white.opacity(0.35), location: 0.10),
                .init(color: .white.opacity(0.00), location: 0.32),
                .init(color: .white.opacity(0.00), location: 1.00),
            ]),
            center: .center
        )
        .rotationEffect(.degrees(turning ? 360 : 0))
        .animation(.linear(duration: period).repeatForever(autoreverses: false), value: turning)
        .onAppear { turning = true }
    }
}

/// The rim's own rotation, an order slower than the sweep. Separate from
/// `Sweep` because it has to hold its angle when it stops rather than snap back
/// to zero, which is what a `.id`-restarted view would do.
private struct SlowTurn: ViewModifier {
    let active: Bool

    @State private var turning = false

    func body(content: Content) -> some View {
        content
            .rotationEffect(.degrees(turning ? 360 : 0))
            .animation(
                active ? .linear(duration: 9).repeatForever(autoreverses: false) : .default,
                value: turning
            )
            .onAppear { turning = active }
            .onChange(of: active) { _, on in turning = on }
    }
}

/// Press feedback for the orb. `.buttonStyle(.plain)` gives none at all, and a
/// control this size with no press state feels broken before it feels calm.
struct MicOrbButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.94 : 1)
            .animation(.spring(response: 0.25, dampingFraction: 0.6), value: configuration.isPressed)
    }
}
