import SwiftUI

/// The email review queue — the only path from your mail to your calendar, and
/// it needs a human at it.
///
/// The risk this screen exists to manage is not that extraction is occasionally
/// wrong. It is that one invented dentist appointment teaches you to distrust
/// the agenda, and an agenda you don't trust is decoration. So the design goal
/// is a *confident* judgement in about a second: what it thinks the event is,
/// when, how sure it is, and the one line of mail it came from — then Accept or
/// Reject, both one tap, neither with a confirmation dialog.
///
/// Accept writes a real event through the mutations log, so it is undoable like
/// anything else. Reject is permanent: that message is never proposed again.
/// The asymmetry is deliberate and the copy says so.
struct ProposalsView: View {
    @EnvironmentObject private var api: JarvisAPI
    @EnvironmentObject private var toasts: ToastCenter

    @State private var proposals: [ProposalsResponse.Proposal]?
    @State private var error: String?
    @State private var isLoading = false
    /// Ids being written. Keeps the card on screen but inert, so a double tap
    /// can't accept twice.
    @State private var deciding: Set<Int> = []

    var body: some View {
        VStack(spacing: 0) {
            ScreenHeader(title: "Review", kicker: "From your mail") {
                if let proposals, !proposals.isEmpty {
                    Text("\(proposals.count) pending")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.text3)
                }
            }
            RefreshStamp(isLoading: isLoading, failed: error != nil)
            content
        }
        .task { await load() }
    }

    @ViewBuilder
    private var content: some View {
        if proposals == nil, let error {
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
        } else if proposals == nil {
            Spacer()
            ProgressView().tint(Theme.accent)
            Spacer()
        } else if let proposals {
            ScrollView {
                LazyVStack(spacing: 12) {
                    if let error {
                        ErrorBanner(message: error)
                    }

                    if proposals.isEmpty {
                        EmptyState(
                            title: "Nothing to review",
                            message: "Jarvis proposes calendar events it finds in your mail. Nothing reaches the agenda without you."
                        )
                    }

                    ForEach(proposals) { proposal in
                        ProposalCard(
                            proposal: proposal,
                            isDeciding: deciding.contains(proposal.id),
                            onAccept: { Task { await decide(proposal, accept: true) } },
                            onReject: { Task { await decide(proposal, accept: false) } }
                        )
                    }

                    if !proposals.isEmpty {
                        Text("Accepting writes a real calendar event, and can be undone. Rejecting is permanent — that message won't be proposed again.")
                            .font(Theme.sans(11.5))
                            .foregroundStyle(Theme.text3)
                            .multilineTextAlignment(.center)
                            .padding(.horizontal, 12)
                            .padding(.top, 6)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.bottom, 24)
            }
            .refreshable { await load() }
        }
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            proposals = try await api.proposals().proposals
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func decide(_ proposal: ProposalsResponse.Proposal, accept: Bool) async {
        guard !deciding.contains(proposal.id) else { return }
        deciding.insert(proposal.id)
        defer { deciding.remove(proposal.id) }

        do {
            let reply = accept
                ? try await api.acceptProposal(proposal.id)
                : try await api.rejectProposal(proposal.id)
            toasts.show(reply)
            withAnimation(.easeOut(duration: 0.2)) {
                proposals?.removeAll { $0.id == proposal.id }
            }
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct ProposalCard: View {
    let proposal: ProposalsResponse.Proposal
    let isDeciding: Bool
    let onAccept: () -> Void
    let onReject: () -> Void

    /// Confidence is shown as a bar with a word, not as a bare percentage.
    /// "0.55" invites arithmetic it can't support; "worth a second look" says
    /// the thing the number is actually for. The percentage stays alongside for
    /// anyone who wants it — anything under 0.5 never reaches the phone, the
    /// server drops it.
    private var confidence: (label: String, tint: Color) {
        switch proposal.confidence {
        case 0.85...: return ("Confident", Theme.success)
        case 0.60..<0.85: return ("Fairly sure", Theme.accent)
        default: return ("Worth a second look", Theme.warning)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(proposal.title)
                    .font(Theme.sans(15.5, weight: .semibold))
                    .foregroundStyle(Theme.text)
                Spacer(minLength: 0)
                Pill(text: proposal.kind, tint: Theme.text2, emphasised: false)
            }

            Text(proposal.when)
                .font(Theme.sans(13))
                .foregroundStyle(Theme.text2)
                .padding(.top, 2)

            if let location = proposal.location, !location.isEmpty {
                Text(location)
                    .font(Theme.sans(13))
                    .foregroundStyle(Theme.text3)
            }

            HStack(spacing: 8) {
                GeometryReader { geometry in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Theme.surface3)
                        Capsule()
                            .fill(confidence.tint)
                            .frame(width: geometry.size.width * proposal.confidence)
                    }
                }
                .frame(height: 5)

                Text("\(Int((proposal.confidence * 100).rounded()))%")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.text3)
            }
            .padding(.top, 12)

            Text(confidence.label)
                .font(Theme.sans(11.5))
                .foregroundStyle(confidence.tint)
                .padding(.top, 5)

            // Provenance, not a summary. `proposals.summary` is templated by
            // the extractor from the title, time and location — the three
            // things already on this card — so printing it would just say
            // everything twice. The design wanted the source email's sender
            // and subject here; those live in `email_messages` and reaching
            // them needs a join `/proposals` doesn't do yet.
            Text("Extracted from \(proposal.source)")
                .font(Theme.sans(12))
                .foregroundStyle(Theme.text3)
                .padding(.top, 8)

            HStack(spacing: 8) {
                Button("Reject", action: onReject)
                    .buttonStyle(OutlineButtonStyle())
                Button("Accept", action: onAccept)
                    .buttonStyle(FilledButtonStyle())
            }
            .padding(.top, 14)
            .disabled(isDeciding)
            .opacity(isDeciding ? 0.5 : 1)
        }
        .jarvisCard(radius: 18, padding: 16)
    }
}
