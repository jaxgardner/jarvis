import Foundation

/// Carries an intent's request into the UI.
///
/// `StartListening` sets a flag here and returns; the app launches, `TalkView`
/// sees the flag and opens the mic. The ordering is not guaranteed — the
/// intent may run before or after the view appears — so this is a latch the
/// view consumes, not an event it has to be present to catch.
@MainActor
final class LaunchRouter: ObservableObject {
    static let shared = LaunchRouter()

    @Published var shouldStartListening = false

    private init() {}

    func requestListening() {
        shouldStartListening = true
    }

    /// Read-and-clear, so a second launch doesn't re-trigger the mic.
    func consumeListeningRequest() -> Bool {
        defer { shouldStartListening = false }
        return shouldStartListening
    }
}
