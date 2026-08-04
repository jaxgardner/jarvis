import Foundation
import UIKit
import UserNotifications

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions options: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        PushRegistrar.registerCategories()
        Task { @MainActor in
            await PushRegistrar.registerIfAlreadyAuthorized()
        }
        return true
    }

    // MARK: - APNs token

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        Task { @MainActor in
            guard JarvisAPI.shared.isEnrolled else { return }
            try? await JarvisAPI.shared.refreshPushToken(hex, label: UIDevice.current.name)
        }
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        // Not fatal and not worth interrupting anyone over: reminders still
        // arrive over ntfy for as long as PUSH_BACKENDS includes it.
        NSLog("APNs registration failed: \(error.localizedDescription)")
    }

    // MARK: - Notification handling

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .sound, .list]
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        let info = response.notification.request.content.userInfo
        let reminderID = info["reminder_id"] as? Int
        // APNs custom payloads survive the round trip as strings often enough
        // that assuming Int here would silently drop the link.
        let rawJob = info["job_id"]
        let jobID = rawJob as? Int ?? Int(rawJob as? String ?? "")

        switch response.actionIdentifier {
        case "SNOOZE":
            guard let reminderID else { return }
            try? await JarvisAPI.shared.snooze(reminder: reminderID, minutes: 10)
        case "DONE":
            guard let reminderID else { return }
            try? await JarvisAPI.shared.ack(reminder: reminderID)
        case "UNDO":
            _ = try? await JarvisAPI.shared.undo()
        case "REPLY":
            // The server 409s if the job is already running again, which is
            // the right answer to a stale notification — nothing to do here
            // but let it fail; the report is unchanged either way.
            guard
                let jobID,
                let typed = (response as? UNTextInputNotificationResponse)?.userText,
                !typed.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            else { return }
            try? await JarvisAPI.shared.reply(toJob: jobID, text: typed)
        default:
            // The notification was tapped. A finished job has somewhere to
            // land now, and landing on whichever tab happened to be showing
            // makes the push pointless.
            if let jobID {
                LaunchRouter.shared.openJob(jobID)
            } else if let kind = info["kind"] as? String,
                      kind == "gratitude" || kind == "brief" {
                // Straight to the mic, for opposite reasons that want the
                // same thing: the gratitude prompt is answered by talking,
                // and the brief is *asked for* by talking. Neither wants a
                // screen in between.
                LaunchRouter.shared.requestListening()
            }
        }
    }
}
