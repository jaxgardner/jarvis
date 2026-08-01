import AppIntents
import Foundation
import SwiftUI

/// App Intents — the surface everything else hangs off.
///
/// This one file is what makes Jarvis reachable from the Action Button, Siri,
/// Spotlight, the Shortcuts app, and (once there is a widget extension) a
/// Control Center control. There is no separate Action Button API: you assign
/// it a Shortcut, and an App Shortcut is a Shortcut.
///
/// The constraint worth knowing before editing `JarvisShortcuts` below: every
/// phrase **must** contain the `\(.applicationName)` token. Omitting it is a
/// compile error, and there is no phrasing that drops the app name. "Hey Siri,
/// talk to Jarvis" is as short as this gets.

// MARK: - Talk

/// Opens the app with the mic already live. This is the one to put on the
/// Action Button — press, talk, done, no aiming at a button on screen.
struct StartListening: AppIntent {
    static let title: LocalizedStringResource = "Talk to Jarvis"
    static let description = IntentDescription(
        "Opens Jarvis and starts listening straight away."
    )

    /// Listening needs the mic and the screen, so this one does open the app.
    /// The other intents deliberately do not.
    static let openAppWhenRun = true

    @MainActor
    func perform() async throws -> some IntentResult {
        LaunchRouter.shared.requestListening()
        return .result()
    }
}

/// The catch-all. Anything you could say to the app, said to Siri instead.
struct SayToJarvis: AppIntent {
    static let title: LocalizedStringResource = "Say to Jarvis"
    static let description = IntentDescription(
        "Sends what you say to Jarvis and speaks the reply."
    )

    @Parameter(title: "Message", requestValueDialog: "What should I tell Jarvis?")
    var text: String

    static var parameterSummary: some ParameterSummary {
        Summary("Tell Jarvis \(\.$text)")
    }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let reply = try await JarvisIntentSupport.say(text)
        return .result(dialog: IntentDialog(stringLiteral: reply))
    }
}

// MARK: - Typed intents

/// Routed through `/say` rather than `POST`ing a reminder directly, so the
/// server's router stays the single place that turns "tomorrow at 8" into an
/// absolute timestamp. Two implementations of relative-time parsing is exactly
/// how a 9 AM reminder ends up at 8.
struct AddReminderIntent: AppIntent {
    static let title: LocalizedStringResource = "Add a reminder"
    static let description = IntentDescription(
        "Captures a reminder, including when it should fire."
    )

    @Parameter(
        title: "Reminder",
        requestValueDialog: "What should I remind you about, and when?"
    )
    var reminder: String

    static var parameterSummary: some ParameterSummary {
        Summary("Remind me to \(\.$reminder)")
    }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog & ShowsSnippetIntent {
        let response = try await JarvisIntentSupport.capture("remind me to \(reminder)")
        return .result(
            dialog: IntentDialog(stringLiteral: response.reply),
            // iOS 26 interactive snippet: the confirmation renders as real UI
            // in the Siri overlay, with Snooze on it. Mis-heard times are the
            // most common voice-capture error, and fixing one without opening
            // the app is the whole point.
            snippetIntent: ReminderSnippet(reply: response.reply, reminderID: response.reminderID ?? 0)
        )
    }
}

/// The view Siri shows after a reminder is captured.
///
/// A `SnippetIntent` is re-run to redraw itself, which is why Snooze can
/// update the card in place: `ReminderSnippet.reload()` re-performs this
/// intent rather than pushing a new one.
struct ReminderSnippet: SnippetIntent {
    static let title: LocalizedStringResource = "Reminder captured"

    @Parameter(title: "Reply")
    var reply: String

    @Parameter(title: "Reminder")
    var reminderID: Int

    init() {}

    init(reply: String, reminderID: Int) {
        self.reply = reply
        self.reminderID = reminderID
    }

    @MainActor
    func perform() async throws -> some IntentResult & ShowsSnippetView {
        .result(view: ReminderSnippetView(reply: reply, reminderID: reminderID))
    }
}

struct ReminderSnippetView: View {
    let reply: String
    let reminderID: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // The accent kicker is the one piece of the app's design language
            // that carries over. The rest is left to the Siri overlay's own
            // material — a card that ignores it reads as a bug, not as brand.
            Label("Reminder set", systemImage: "bell.badge.fill")
                .font(Theme.mono(11, weight: .semibold))
                .textCase(.uppercase)
                .foregroundStyle(Theme.accent)

            Text(reply)
                .font(.headline)
                .fixedSize(horizontal: false, vertical: true)

            if reminderID > 0 {
                Button(intent: SnoozeFromSnippet(reminderID: reminderID)) {
                    Label("Snooze 10 minutes", systemImage: "clock.arrow.circlepath")
                }
                .buttonStyle(.bordered)
            }
        }
        .padding()
    }
}

/// Snooze, invoked from the snippet's button. Reloads the card rather than
/// opening the app.
struct SnoozeFromSnippet: AppIntent {
    static let title: LocalizedStringResource = "Snooze this reminder"
    static let isDiscoverable = false  // reachable from the snippet, not from Shortcuts

    @Parameter(title: "Reminder")
    var reminderID: Int

    init() {}

    init(reminderID: Int) {
        self.reminderID = reminderID
    }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        try await JarvisAPI.shared.snooze(reminder: reminderID, minutes: 10)
        ReminderSnippet.reload()
        return .result(dialog: "Snoozed ten minutes.")
    }
}

struct WhatsMyAgenda: AppIntent {
    static let title: LocalizedStringResource = "Check my agenda"
    static let description = IntentDescription("Reads out what's coming up today.")

    @Parameter(title: "Days", default: 1)
    var days: Int

    static var parameterSummary: some ParameterSummary {
        Summary("What's on for the next \(\.$days) day(s)")
    }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard JarvisAPI.shared.isEnrolled else {
            return .result(dialog: IntentDialog(stringLiteral: JarvisIntentSupport.notSetUp))
        }
        let agenda = try await JarvisAPI.shared.agenda(days: max(1, days))
        return .result(dialog: IntentDialog(stringLiteral: Self.spoken(agenda)))
    }

    /// Composed from the server's `when` strings, never re-formatted here.
    /// The server already renders "tomorrow at 3 PM"; a second implementation
    /// of that would drift from the first.
    private static func spoken(_ agenda: AgendaResponse) -> String {
        var parts: [String] = []
        for event in agenda.events {
            let place = event.location.map { " at \($0)" } ?? ""
            parts.append("\(event.title) \(event.when)\(place)")
        }
        for reminder in agenda.reminders {
            parts.append("a reminder to \(reminder.body) \(reminder.when)")
        }
        guard !parts.isEmpty else { return "Nothing on. You're clear." }
        return "You have " + JarvisIntentSupport.join(parts) + "."
    }
}

struct UndoLastIntent: AppIntent {
    static let title: LocalizedStringResource = "Undo the last change"
    static let description = IntentDescription("Reverses the most recent change.")

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard JarvisAPI.shared.isEnrolled else {
            return .result(dialog: IntentDialog(stringLiteral: JarvisIntentSupport.notSetUp))
        }
        let result = try await JarvisAPI.shared.undo()
        return .result(dialog: IntentDialog(stringLiteral: result["reply"] ?? "Undone."))
    }
}

// MARK: - Shared bits

enum JarvisIntentSupport {
    static let notSetUp = "Jarvis isn't set up on this phone yet. Open the app to enroll."

    @MainActor
    static func say(_ text: String) async throws -> String {
        guard JarvisAPI.shared.isEnrolled else { return notSetUp }
        return try await JarvisAPI.shared.say(text).reply
    }

    /// Like `say`, but keeps the whole response — the snippet needs to know
    /// which row was created, not just what to read out.
    ///
    /// Throws rather than returning the not-set-up sentence, because this
    /// caller renders a snippet: there is no card to draw for a device that
    /// hasn't enrolled, and Siri surfaces a thrown error's description anyway.
    @MainActor
    static func capture(_ text: String) async throws -> SayResponse {
        guard JarvisAPI.shared.isEnrolled else { throw NotEnrolled() }
        return try await JarvisAPI.shared.say(text)
    }

    struct NotEnrolled: LocalizedError {
        var errorDescription: String? { notSetUp }
    }

    /// Natural spoken list, matching the server's `_join`.
    static func join(_ parts: [String]) -> String {
        switch parts.count {
        case 1: return parts[0]
        case 2: return "\(parts[0]) and \(parts[1])"
        default: return parts.dropLast().joined(separator: ", ") + ", and " + parts[parts.count - 1]
        }
    }
}

// MARK: - Phrases

struct JarvisShortcuts: AppShortcutsProvider {
    /// Every phrase has to carry `\(.applicationName)`. Not a style choice —
    /// it will not compile otherwise.
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: StartListening(),
            phrases: [
                "Talk to \(.applicationName)",
                "Hey \(.applicationName)",
                "Open \(.applicationName)",
            ],
            shortTitle: "Talk",
            systemImageName: "mic.fill"
        )
        AppShortcut(
            intent: SayToJarvis(),
            phrases: [
                "Tell \(.applicationName)",
                "Ask \(.applicationName)",
            ],
            shortTitle: "Say",
            systemImageName: "bubble.left.and.text.bubble.right"
        )
        AppShortcut(
            intent: AddReminderIntent(),
            phrases: [
                "Add a \(.applicationName) reminder",
                "New \(.applicationName) reminder",
            ],
            shortTitle: "Remind",
            systemImageName: "bell.badge"
        )
        AppShortcut(
            intent: WhatsMyAgenda(),
            phrases: [
                "What's on my \(.applicationName) agenda",
                "\(.applicationName) agenda",
            ],
            shortTitle: "Agenda",
            systemImageName: "calendar"
        )
        AppShortcut(
            intent: UndoLastIntent(),
            phrases: [
                "\(.applicationName) undo that",
                "Undo that in \(.applicationName)",
            ],
            shortTitle: "Undo",
            systemImageName: "arrow.uturn.backward"
        )
    }
}
