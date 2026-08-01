import SwiftUI
import UIKit

/// One-time enrollment: trade the shared token for a token belonging to this
/// device. Run once per install.
struct SetupView: View {
    @EnvironmentObject private var api: JarvisAPI

    @State private var host = ""
    @State private var sharedToken = ""
    @State private var label = UIDevice.current.name
    @State private var isWorking = false
    @State private var error: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Set up Jarvis")
                        .font(Theme.sans(26, weight: .medium))
                        .tracking(-0.3)
                        .foregroundStyle(Theme.text)
                    Text("Enter the Mini's address and the one-time setup token from its terminal.")
                        .font(Theme.sans(13.5))
                        .foregroundStyle(Theme.text3)
                }
                .padding(.bottom, 4)

                field(
                    "Mini address",
                    // Named, not numbered. An App Intent runs in a separate
                    // extension process; if it holds a stale IP the Siri path
                    // breaks while the app keeps working, which is a miserable
                    // thing to debug.
                    note: "Use the MagicDNS name, not an IP address."
                ) {
                    TextField("mini.your-tailnet.ts.net", text: $host)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .font(Theme.mono(14))
                }

                field(
                    "Setup token",
                    note: "Used once, to enroll. This phone gets its own token afterwards, so losing it costs one revocation instead of re-keying everything."
                ) {
                    SecureField("JARVIS_TOKEN", text: $sharedToken)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(Theme.mono(14))
                }

                field("Device label") {
                    TextField("Label", text: $label)
                        .font(Theme.sans(14))
                }

                if let error {
                    ErrorBanner(message: error)
                }

                Button {
                    Task { await enroll() }
                } label: {
                    if isWorking {
                        ProgressView().tint(Theme.onAccent)
                    } else {
                        Text("Enroll this device")
                    }
                }
                .buttonStyle(FilledButtonStyle())
                .disabled(isWorking || host.isEmpty || sharedToken.isEmpty)
                .opacity(host.isEmpty || sharedToken.isEmpty ? 0.5 : 1)
                .padding(.top, 8)
            }
            .padding(.horizontal, 28)
            .padding(.vertical, 40)
        }
        .jarvisBackground()
        .preferredColorScheme(.dark)
        .tint(Theme.accent)
    }

    private func field<Content: View>(
        _ title: String,
        note: String? = nil,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            SectionLabel(text: title)
            content()
                .foregroundStyle(Theme.text)
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
                .background(
                    Theme.surface,
                    in: RoundedRectangle(cornerRadius: 10, style: .continuous)
                )
                .overlay {
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .strokeBorder(Theme.border, lineWidth: 1)
                }
            if let note {
                Text(note)
                    .font(Theme.sans(11.5))
                    .foregroundStyle(Theme.text3)
            }
        }
    }

    private func enroll() async {
        isWorking = true
        error = nil
        defer { isWorking = false }
        do {
            try await api.enroll(host: host, sharedToken: sharedToken, label: label)
            // Ask for push only after enrollment — the prompt makes no sense
            // before there is a server to send anything.
            await PushRegistrar.requestAuthorizationAndRegister()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

/// Where the endpointing preference lives. `UserDefaults` rather than the
/// Keychain — it is a preference, not a secret — and named in one place so the
/// view that reads it and the view that writes it cannot drift apart.
enum VoiceSettings {
    static let autoSendKey = "jarvis.autoSend"
    static let pauseKey = "jarvis.pauseToSend"

    /// A second of true silence is already a long gap in speech; shorter than
    /// this and an ordinary mid-sentence breath sends half a reminder.
    static let defaultPause: Double = 1.2

    static let choices: [(label: String, seconds: Double)] = [
        ("Quick 0.8s", 0.8),
        ("Normal 1.2s", 1.2),
        ("Relaxed 2.0s", 2.0),
    ]
}

/// Post-enrollment settings. Deliberately thin: this is not the dashboard.
struct SettingsView: View {
    @EnvironmentObject private var api: JarvisAPI
    @Environment(\.dismiss) private var dismiss
    @State private var confirmingForget = false

    @AppStorage(VoiceSettings.autoSendKey) private var autoSend = true
    @AppStorage(VoiceSettings.pauseKey) private var pauseToSend = VoiceSettings.defaultPause

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Capsule()
                .fill(Color.white.opacity(0.18))
                .frame(width: 36, height: 4)
                .frame(maxWidth: .infinity)
                .padding(.top, 10)

            HStack {
                Text("Settings")
                    .font(Theme.sans(18, weight: .bold))
                    .foregroundStyle(Theme.text)
                Spacer()
                Button {
                    dismiss()
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Theme.text2)
                        .frame(width: 28, height: 28)
                        .background(Theme.surface, in: Circle())
                }
                .buttonStyle(.plain)
            }

            Toggle(isOn: $autoSend) {
                Text("Send when I stop talking")
                    .font(Theme.sans(14.5))
                    .foregroundStyle(Theme.text)
            }
            .tint(Theme.accent)

            if autoSend {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Pause length")
                        .font(Theme.sans(13))
                        .foregroundStyle(Theme.text3)
                    SegmentedBar(
                        options: VoiceSettings.choices.map { ($0.label, $0.seconds) },
                        selection: $pauseToSend
                    )
                }
            }

            // Worth stating: the button does not go away, and in a loud room it
            // is the reliable path.
            Text(autoSend
                 ? "Tapping the mic still sends immediately."
                 : "The mic stays on until you tap the button again.")
                .font(Theme.sans(11.5))
                .foregroundStyle(Theme.text3)

            Divider().overlay(Theme.border)

            HStack {
                Text("Mini address")
                    .font(Theme.sans(13))
                    .foregroundStyle(Theme.text3)
                Spacer()
                Text(api.host)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.text2)
            }

            Button {
                if let url = URL(string: UIApplication.openSettingsURLString) {
                    UIApplication.shared.open(url)
                }
            } label: {
                Text("Notification settings ›")
                    .font(Theme.sans(14))
                    .foregroundStyle(Theme.accent)
            }
            .buttonStyle(.plain)

            Spacer(minLength: 0)

            if confirmingForget {
                Button {
                    api.forget()
                    dismiss()
                } label: {
                    Text("Tap again to confirm — Forget this device")
                        .font(Theme.sans(14))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 11)
                        .background(
                            Theme.dim(Theme.danger),
                            in: RoundedRectangle(cornerRadius: 10, style: .continuous)
                        )
                        .overlay {
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .strokeBorder(Theme.danger, lineWidth: 1)
                        }
                        .foregroundStyle(Theme.danger)
                }
                .buttonStyle(.plain)
            } else {
                Button {
                    withAnimation(.easeOut(duration: 0.15)) { confirmingForget = true }
                } label: {
                    Text("Forget this device")
                        .font(Theme.sans(14))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 11)
                        .overlay {
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .strokeBorder(Theme.border, lineWidth: 1)
                        }
                        .foregroundStyle(Theme.danger)
                }
                .buttonStyle(.plain)
            }

            // Forgetting is local. Saying so avoids the false sense that a lost
            // phone has been dealt with when only this one has.
            Text("Removes this phone's token from the Keychain. Revoke it on the server too — Health › Devices — or it stays valid.")
                .font(Theme.sans(11))
                .foregroundStyle(Theme.text3)
                .padding(.bottom, 12)
        }
        .padding(.horizontal, 20)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background {
            LinearGradient(
                colors: [Theme.surface, Theme.bg],
                startPoint: .top,
                endPoint: .bottom
            )
            .ignoresSafeArea()
        }
        .presentationDetents([.height(470)])
        .presentationDragIndicator(.hidden)
        .presentationBackground(.clear)
    }
}
