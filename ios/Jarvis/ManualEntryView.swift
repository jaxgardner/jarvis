import SwiftUI

/// Type what is in the house, one item per line.
///
/// Built for the case that actually happens: a baseline stocktake on day one,
/// where you are standing at an open fridge with thirty things in it and no
/// receipt for any of them. One text box you can dictate or paste into beats
/// thirty round trips through a form.
///
/// It deliberately stops at the review screen rather than writing straight to
/// the inventory. The dates are still being *proposed* by the shelf-life
/// table, so this is the same act as reviewing a photographed receipt — and
/// the same one Confirm, one undo.
struct ManualEntryView: View {
    /// Handed the new receipt id once the batch exists server-side.
    let onCreated: (Int) -> Void

    @EnvironmentObject private var api: JarvisAPI
    @EnvironmentObject private var toasts: ToastCenter
    @Environment(\.dismiss) private var dismiss

    @State private var text = ""
    @State private var isSaving = false

    private var names: [String] {
        text.split(separator: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }

    var body: some View {
        VStack(spacing: 0) {
            ScreenHeader(title: "Add items", kicker: "One per line") {
                Button("Cancel") { dismiss() }
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.text3)
            }

            TextEditor(text: $text)
                .font(Theme.sans(16))
                .foregroundStyle(Theme.text)
                .scrollContentBackground(.hidden)
                .background(Theme.surface)
                .clipShape(RoundedRectangle(cornerRadius: 10))
                .padding(.horizontal, 16)
                .overlay(alignment: .topLeading) {
                    if text.isEmpty {
                        Text("whole milk\neggs\nspinach\nchicken breast\nrice")
                            .font(Theme.sans(16))
                            .foregroundStyle(Theme.text3)
                            .padding(.horizontal, 21)
                            .padding(.top, 8)
                            .allowsHitTesting(false)
                    }
                }

            Button(buttonLabel) { Task { await create() } }
                .buttonStyle(.borderedProminent)
                .tint(Theme.accent)
                .disabled(names.isEmpty || isSaving)
                .padding(16)
        }
        .jarvisBackground()
    }

    private var buttonLabel: String {
        if isSaving { return "Adding…" }
        if names.isEmpty { return "Review" }
        return names.count == 1 ? "Review 1 item" : "Review \(names.count) items"
    }

    private func create() async {
        isSaving = true
        defer { isSaving = false }
        do {
            let created = try await api.createManualReceipt(names)
            dismiss()
            onCreated(created.receiptId)
        } catch {
            toasts.show(Failure.reason(error.localizedDescription))
        }
    }
}
