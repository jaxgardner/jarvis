import Foundation
import UIKit
import UserNotifications

/// APNs registration and the notification action buttons.
///
/// This is the client half of the contract `scheduler/run.py` already sends:
/// a fired reminder arrives with `category: "REMINDER"` and a `reminder_id`
/// in its custom keys. Without the category registered here, those buttons
/// never appear and the server-side snooze/ack endpoints are dead code.
enum PushRegistrar {
    static let reminderCategory = "REMINDER"
    static let jobCategory = "JOB"

    static func registerCategories() {
        let snooze = UNNotificationAction(
            identifier: "SNOOZE",
            title: "Snooze 10m",
            options: []
        )
        let done = UNNotificationAction(
            identifier: "DONE",
            title: "Done",
            options: []
        )
        // Voice input is lossy — the reminder you're looking at may be one you
        // never meant to create. Undo belongs on the notification itself.
        let undo = UNNotificationAction(
            identifier: "UNDO",
            title: "Undo",
            options: [.destructive]
        )

        let reminder = UNNotificationCategory(
            identifier: reminderCategory,
            actions: [snooze, done, undo],
            intentIdentifiers: [],
            options: []
        )
        // A report that ends in a question usually finds you somewhere other
        // than the app. iOS gives the inline field for free, and answering
        // from the lock screen is the surface that matters most.
        let reply = UNTextInputNotificationAction(
            identifier: "REPLY",
            title: "Reply",
            options: [],
            textInputButtonTitle: "Send",
            textInputPlaceholder: "Answer this report"
        )
        let job = UNNotificationCategory(
            identifier: jobCategory,
            actions: [reply],
            intentIdentifiers: [],
            options: []
        )
        UNUserNotificationCenter.current().setNotificationCategories([reminder, job])
    }

    @MainActor
    static func requestAuthorizationAndRegister() async {
        let center = UNUserNotificationCenter.current()
        let granted = (try? await center.requestAuthorization(
            options: [.alert, .sound, .badge]
        )) ?? false
        guard granted else { return }
        UIApplication.shared.registerForRemoteNotifications()
    }

    /// Register on every launch, not just after the permission prompt: iOS
    /// re-issues device tokens on reinstall and restore-from-backup, and a
    /// stale token fails silently — which is the worst way for push to break.
    @MainActor
    static func registerIfAlreadyAuthorized() async {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        guard settings.authorizationStatus == .authorized
                || settings.authorizationStatus == .provisional
        else { return }
        UIApplication.shared.registerForRemoteNotifications()
    }
}
