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
        NavigationStack {
            Form {
                Section {
                    TextField("mini.your-tailnet.ts.net", text: $host)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                } header: {
                    Text("Mini address")
                } footer: {
                    // Named, not numbered. An App Intent runs in a separate
                    // extension process; if it holds a stale IP the Siri path
                    // breaks while the app keeps working, which is a miserable
                    // thing to debug.
                    Text("Use the MagicDNS name, not an IP address.")
                }

                Section {
                    SecureField("JARVIS_TOKEN", text: $sharedToken)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Setup token")
                } footer: {
                    Text("Used once, to enroll. This phone gets its own token afterwards, so losing it costs one revocation instead of re-keying everything.")
                }

                Section("This device") {
                    TextField("Label", text: $label)
                }

                if let error {
                    Text(error).foregroundStyle(.red)
                }

                Section {
                    Button {
                        Task { await enroll() }
                    } label: {
                        if isWorking {
                            ProgressView()
                        } else {
                            Text("Enroll")
                        }
                    }
                    .disabled(isWorking || host.isEmpty || sharedToken.isEmpty)
                }
            }
            .navigationTitle("Set up Jarvis")
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
        ("Quick", 0.8),
        ("Normal", 1.2),
        ("Relaxed", 2.0),
    ]
}

/// Post-enrollment settings. Deliberately thin: this is not the dashboard,
/// which is Phase 7d.
struct SettingsView: View {
    @EnvironmentObject private var api: JarvisAPI
    @Environment(\.dismiss) private var dismiss
    @State private var confirmingForget = false

    @AppStorage(VoiceSettings.autoSendKey) private var autoSend = true
    @AppStorage(VoiceSettings.pauseKey) private var pauseToSend = VoiceSettings.defaultPause

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Toggle("Send when I stop talking", isOn: $autoSend)

                    if autoSend {
                        Picker("Pause length", selection: $pauseToSend) {
                            ForEach(VoiceSettings.choices, id: \.seconds) { choice in
                                Text(choice.label).tag(choice.seconds)
                            }
                        }
                        .pickerStyle(.segmented)
                    }
                } header: {
                    Text("Voice")
                } footer: {
                    // Worth stating: the button does not go away, and in a loud
                    // room it is the reliable path.
                    Text(autoSend
                         ? "Tap the mic, talk, and Jarvis sends when you pause. Tapping the button still sends immediately."
                         : "The mic stays on until you tap the button again.")
                }

                Section("Mini address") {
                    Text(api.host).foregroundStyle(.secondary)
                }
                Section {
                    Button("Notifications settings") {
                        if let url = URL(string: UIApplication.openSettingsURLString) {
                            UIApplication.shared.open(url)
                        }
                    }
                }
                Section {
                    Button("Forget this device", role: .destructive) {
                        confirmingForget = true
                    }
                } footer: {
                    Text("Removes this phone's token from the Keychain. Revoke it on the server too — DELETE /devices/{id} — or it stays valid.")
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
            .confirmationDialog(
                "Forget this device?",
                isPresented: $confirmingForget,
                titleVisibility: .visible
            ) {
                Button("Forget", role: .destructive) {
                    api.forget()
                    dismiss()
                }
            }
        }
    }
}
