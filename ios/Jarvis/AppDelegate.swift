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

        switch response.actionIdentifier {
        case "SNOOZE":
            guard let reminderID else { return }
            try? await JarvisAPI.shared.snooze(reminder: reminderID, minutes: 10)
        case "DONE":
            guard let reminderID else { return }
            try? await JarvisAPI.shared.ack(reminder: reminderID)
        case "UNDO":
            _ = try? await JarvisAPI.shared.undo()
        default:
            // The notification was tapped. A finished job has somewhere to
            // land now, and landing on whichever tab happened to be showing
            // makes the push pointless.
            // APNs custom payloads survive the round trip as strings often
            // enough that assuming Int here would silently drop the link.
            let raw = info["job_id"]
            if let jobID = raw as? Int ?? Int(raw as? String ?? "") {
                LaunchRouter.shared.openJob(jobID)
            }
        }
    }
}
