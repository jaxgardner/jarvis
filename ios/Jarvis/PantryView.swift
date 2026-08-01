import SwiftUI

/// What is in the house, what is about to die, and what to buy.
///
/// Capture is the primary action and sits at the top: the whole feature
/// starts with a photograph, and burying that behind a scroll would make the
/// app slower than not using it.
struct PantryView: View {
    @EnvironmentObject private var api: JarvisAPI
    @EnvironmentObject private var toasts: ToastCenter

    @State private var pantry: PantryResponse?
    @State private var error: String?
    @State private var isLoading = false
    @State private var showingCamera = false
    @State private var reviewing: Int?
    @State private var newEntry = ""

    var body: some View {
        VStack(spacing: 0) {
            ScreenHeader(title: "Pantry", kicker: "What's in the house") {
                Button {
                    showingCamera = true
                } label: {
                    Label("Receipt", systemImage: "camera.fill")
                        .font(Theme.mono(12))
                }
                .tint(Theme.accent)
            }
            RefreshStamp(isLoading: isLoading, failed: error != nil)
            content
        }
        .task { await load() }
        .sheet(isPresented: $showingCamera) {
            ReceiptCamera { jpeg in Task { await upload(jpeg) } }
        }
        .sheet(item: Binding(
            get: { reviewing.map(ReviewTarget.init) },
            set: { reviewing = $0?.id }
        )) { target in
            ReceiptReviewView(receiptId: target.id) {
                reviewing = nil
                Task { await load() }
            }
            .environmentObject(api)
            .environmentObject(toasts)
        }
    }

    private struct ReviewTarget: Identifiable { let id: Int }

    @ViewBuilder
    private var content: some View {
        if pantry == nil, let error {
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
        } else if let pantry {
            List {
                if !pantry.shoppingList.isEmpty {
                    Section("Shopping list") {
                        ForEach(pantry.shoppingList) { entry in
                            HStack {
                                Text(entry.name).font(Theme.sans(15))
                                Spacer()
                                if let reason = entry.reason, reason != "manual" {
                                    Text(reason)
                                        .font(Theme.mono(10))
                                        .foregroundStyle(Theme.text3)
                                }
                            }
                            .swipeActions {
                                Button("Got it") {
                                    Task { await resolve(entry.id) }
                                }
                                .tint(Theme.success)
                            }
                            .listRowBackground(Theme.surface)
                        }
                    }
                }

                Section("In the house") {
                    if pantry.items.isEmpty {
                        Text("Nothing tracked yet. Photograph a receipt.")
                            .font(Theme.sans(14))
                            .foregroundStyle(Theme.text3)
                            .listRowBackground(Theme.surface)
                    }
                    ForEach(pantry.items) { item in
                        PantryItemRow(item: item)
                            .listRowBackground(Theme.surface)
                    }
                }
            }
            .listStyle(.insetGrouped)
            .scrollContentBackground(.hidden)
            .refreshable { await load() }
        } else {
            Spacer()
            ProgressView().tint(Theme.accent)
            Spacer()
        }
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            pantry = try await api.pantry()
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func upload(_ jpeg: Data) async {
        do {
            let response = try await api.uploadReceipt(jpeg)
            reviewing = response.receiptId
        } catch {
            toasts.show(Failure.reason(error.localizedDescription))
        }
    }

    private func resolve(_ id: Int) async {
        do {
            try await api.resolveShoppingEntry(id)
            await load()
        } catch {
            toasts.show(Failure.reason(error.localizedDescription))
        }
    }
}

/// One item in the fridge. Urgency is carried by colour so the list can be
/// read at a glance rather than word by word.
struct PantryItemRow: View {
    let item: PantryResponse.Item

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 3) {
                Text(item.name)
                    .font(Theme.sans(15, weight: .medium))
                    .foregroundStyle(Theme.text)
                Text(item.location)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.text3)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(freshness)
                    .font(Theme.mono(12))
                    .foregroundStyle(urgency)
                if item.expirySource == "default" {
                    // Says which dates you actually stand behind. A guessed
                    // date that looks identical to a confirmed one is how the
                    // inventory quietly stops being trustworthy.
                    Text("estimated")
                        .font(Theme.mono(9))
                        .foregroundStyle(Theme.text3)
                }
            }
        }
        .padding(.vertical, 4)
    }

    private var freshness: String {
        guard let days = item.daysLeft else { return "—" }
        if days < 0 { return "\(-days)d overdue" }
        if days == 0 { return "today" }
        return "\(days)d"
    }

    private var urgency: Color {
        guard let days = item.daysLeft else { return Theme.text3 }
        if days < 0 { return Theme.danger }
        if days <= 2 { return Theme.warning }
        return Theme.text2
    }
}
