import SwiftUI

/// Everything holding a token for this Mini.
///
/// The reason per-device tokens exist is that losing your phone should cost one
/// revocation rather than re-keying every client — which only pays off if
/// revocation is somewhere you can find it under stress. So it is two taps and
/// unambiguous: the button says what it will do, then asks once, and the row
/// stays visible afterwards struck through rather than vanishing, because a
/// device disappearing from the list is indistinguishable from the request
/// having failed.
struct DevicesView: View {
    @EnvironmentObject private var api: JarvisAPI
    @EnvironmentObject private var toasts: ToastCenter

    @State private var devices: [DevicesResponse.Device]?
    @State private var error: String?
    @State private var confirming: Int?

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                if let error {
                    ErrorBanner(message: error)
                }

                if let devices {
                    if devices.isEmpty {
                        EmptyState(
                            title: "No devices",
                            message: "Nothing has enrolled against this server."
                        )
                    }
                    ForEach(devices) { device in
                        DeviceCard(
                            device: device,
                            isConfirming: confirming == device.id,
                            onTap: { tapRevoke(device) }
                        )
                    }

                    if !devices.isEmpty {
                        Text("Revoking is immediate and one-way. A revoked device has to enroll again with the shared token.")
                            .font(Theme.sans(11.5))
                            .foregroundStyle(Theme.text3)
                            .multilineTextAlignment(.center)
                            .padding(.top, 6)
                    }
                } else if error == nil {
                    ProgressView().tint(Theme.accent).padding(.top, 60)
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 16)
        }
        .jarvisBackground()
        .navigationTitle("Devices")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { await load() }
    }

    private func tapRevoke(_ device: DevicesResponse.Device) {
        guard !device.isRevoked else { return }
        if confirming == device.id {
            Task { await revoke(device) }
        } else {
            withAnimation(.easeOut(duration: 0.15)) { confirming = device.id }
        }
    }

    private func load() async {
        do {
            devices = try await api.devices().devices
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func revoke(_ device: DevicesResponse.Device) async {
        confirming = nil
        do {
            try await api.revokeDevice(device.id)
            toasts.show("Revoked \(device.label)")
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct DeviceCard: View {
    let device: DevicesResponse.Device
    let isConfirming: Bool
    let onTap: () -> Void

    private var subtitle: String {
        var parts: [String] = [device.platform ?? "unknown"]
        if device.canPush, let env = device.apnsEnv { parts.append("apns \(env)") }
        if let seen = device.lastSeenAt {
            parts.append("seen \(RelativeStamp.short(seen))")
        } else {
            parts.append("never seen")
        }
        return parts.joined(separator: " · ")
    }

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text(device.label)
                    .font(Theme.sans(14.5, weight: .semibold))
                    .strikethrough(device.isRevoked, color: Theme.text3)
                    .foregroundStyle(device.isRevoked ? Theme.text3 : Theme.text)
                Text(subtitle)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.text3)
            }

            Spacer(minLength: 0)

            Button(action: onTap) {
                Text(device.isRevoked ? "Revoked" : (isConfirming ? "Confirm?" : "Revoke"))
                    .font(Theme.sans(11.5, weight: .medium))
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .background(
                        isConfirming ? Theme.danger : .clear,
                        in: RoundedRectangle(cornerRadius: 8, style: .continuous)
                    )
                    .overlay {
                        RoundedRectangle(cornerRadius: 8, style: .continuous)
                            .strokeBorder(
                                device.isRevoked ? Theme.border : Theme.danger,
                                lineWidth: 1
                            )
                    }
                    .foregroundStyle(
                        device.isRevoked
                            ? Theme.text3
                            : (isConfirming ? Theme.onAccent : Theme.danger)
                    )
            }
            .buttonStyle(.plain)
            .disabled(device.isRevoked)
        }
        .jarvisCard()
    }
}
