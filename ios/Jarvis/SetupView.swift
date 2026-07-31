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

/// Post-enrollment settings. Deliberately thin: this is not the dashboard,
/// which is Phase 7d.
struct SettingsView: View {
    @EnvironmentObject private var api: JarvisAPI
    @Environment(\.dismiss) private var dismiss
    @State private var confirmingForget = false

    var body: some View {
        NavigationStack {
            Form {
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
