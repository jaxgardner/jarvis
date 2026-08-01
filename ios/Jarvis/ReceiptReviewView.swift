import SwiftUI

/// The human gate between a photographed receipt and the fridge.
///
/// The design goal is that a normal shopping trip costs two or three taps.
/// Dates arrive pre-filled from the shelf-life table and perishables sort to
/// the top, so the interaction is *skim and correct*, not *fill in a form* —
/// entering a date for every item on every trip is the exact friction that
/// makes people abandon pantry apps around week three.
///
/// Nothing here is destructive without being reversible: Confirm writes
/// through the mutations log as a single unit, so one /undo reverses the whole
/// trip. Discard is the only one-way door and it only applies to a receipt you
/// have not confirmed.
struct ReceiptReviewView: View {
    let receiptId: Int
    let onFinish: () -> Void

    @EnvironmentObject private var api: JarvisAPI
    @EnvironmentObject private var toasts: ToastCenter

    @State private var detail: ReceiptDetail?
    @State private var error: String?
    @State private var edits: [Int: [String: Any]] = [:]
    @State private var deleted: Set<Int> = []
    @State private var isSaving = false

    var body: some View {
        VStack(spacing: 0) {
            ScreenHeader(title: "Review items", kicker: kicker) {
                if let detail, !detail.items.isEmpty {
                    Text("\(detail.items.count - deleted.count) items")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.text3)
                }
            }
            content
            if detail?.status == "pending" { actions }
        }
        .jarvisBackground()
        .task { await poll() }
    }

    /// A manual batch has no store and never had a camera anywhere near it,
    /// so "From your camera" would be a small lie on the one screen whose job
    /// is to be trusted.
    private var kicker: String {
        if let store = detail?.store, !store.isEmpty { return store }
        return detail?.source == "manual" ? "Typed by hand" : "From your camera"
    }

    @ViewBuilder
    private var content: some View {
        if let detail {
            if let extractError = detail.extractError, detail.items.isEmpty {
                // A receipt that looks like it had nothing on it is the worst
                // outcome. Saying what went wrong is strictly better.
                ErrorState(
                    title: "Couldn't read this receipt",
                    detail: extractError,
                    hint: "Retry, or discard it and shoot it again in better light.",
                    retry: { Task { await reload() } }
                )
                .padding(.top, 60)
            } else {
                List {
                    ForEach(detail.items.filter { !deleted.contains($0.id) }) { item in
                        ReceiptItemRow(
                            item: item,
                            expiresOn: binding(for: item),
                            onDelete: { deleted.insert(item.id) }
                        )
                        .listRowBackground(Theme.surface)
                    }
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
            }
        } else if let error {
            ErrorState(
                title: "Can't reach the Mini",
                detail: Failure.reason(error),
                hint: "Usually means the private network (Tailscale) is off.",
                retry: { Task { await reload() } }
            )
            .padding(.top, 60)
        } else {
            Spacer()
            VStack(spacing: 12) {
                ProgressView().tint(Theme.accent)
                Text("Reading the receipt…")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.text3)
            }
            Spacer()
        }
    }

    private var actions: some View {
        HStack(spacing: 12) {
            Button("Discard") { Task { await discard() } }
                .buttonStyle(.bordered)
                .tint(Theme.danger)
            Button(isSaving ? "Saving…" : "Confirm") { Task { await confirm() } }
                .buttonStyle(.borderedProminent)
                .tint(Theme.accent)
                .disabled(isSaving)
        }
        .padding(16)
    }

    /// Edits are held locally and flushed once on Confirm. A PATCH per
    /// keystroke would make correcting three dates into nine round trips over
    /// a home uplink.
    private func binding(for item: PantryResponse.Item) -> Binding<String> {
        Binding(
            get: { edits[item.id]?["expires_on"] as? String ?? item.expiresOn ?? "" },
            set: { edits[item.id, default: [:]]["expires_on"] = $0 }
        )
    }

    /// Extraction runs behind the upload, so the first load polls — the same
    /// contract the Jobs screen uses for the deep path.
    private func poll() async {
        for _ in 0..<20 {
            await reload()
            if detail?.status != "extracting" { return }
            try? await Task.sleep(for: .seconds(1))
        }
    }

    private func reload() async {
        do {
            detail = try await api.receipt(receiptId)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func confirm() async {
        isSaving = true
        defer { isSaving = false }
        var payload: [[String: Any]] = edits.map { id, values in
            values.merging(["id": id]) { current, _ in current }
        }
        payload += deleted.map { ["id": $0, "delete": true] }

        do {
            if !payload.isEmpty {
                try await api.patchReceiptItems(receiptId, edits: payload)
            }
            toasts.show(try await api.confirmReceipt(receiptId))
            onFinish()
        } catch {
            toasts.show(Failure.reason(error.localizedDescription))
        }
    }

    private func discard() async {
        do {
            try await api.discardReceipt(receiptId)
            toasts.show("Receipt discarded.")
            onFinish()
        } catch {
            toasts.show(Failure.reason(error.localizedDescription))
        }
    }
}

/// One line of the receipt. The date field is the only thing here that
/// usually needs a decision, so it gets the visual weight.
struct ReceiptItemRow: View {
    let item: PantryResponse.Item
    @Binding var expiresOn: String
    let onDelete: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(item.name)
                    .font(Theme.sans(15, weight: .medium))
                    .foregroundStyle(Theme.text)
                if let raw = item.rawText, raw.caseInsensitiveCompare(item.name) != .orderedSame {
                    // The receipt's own words, kept visible: it is what you
                    // check against when a name looks wrong.
                    Text(raw)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.text3)
                }
                Text(item.location)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.text3)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 4) {
                if expiresOn.isEmpty {
                    Text("no date")
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.text3)
                } else {
                    DatePicker(
                        "",
                        selection: dateBinding,
                        displayedComponents: .date
                    )
                    .labelsHidden()
                    .datePickerStyle(.compact)
                }
                Button("Remove", action: onDelete)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.danger)
            }
        }
        .padding(.vertical, 6)
    }

    private var dateBinding: Binding<Date> {
        Binding(
            get: { Self.formatter.date(from: expiresOn) ?? Date() },
            set: { expiresOn = Self.formatter.string(from: $0) }
        )
    }

    /// The server speaks bare `YYYY-MM-DD` for `_on` columns. Fixed to a
    /// POSIX locale so a user's regional format cannot produce a date the API
    /// rejects.
    static let formatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        return formatter
    }()
}
