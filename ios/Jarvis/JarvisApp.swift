import SwiftUI

@main
struct JarvisApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var api = JarvisAPI.shared

    var body: some Scene {
        WindowGroup {
            Group {
                if api.isEnrolled {
                    RootView()
                } else {
                    SetupView()
                }
            }
            .environmentObject(api)
        }
    }
}

/// Talk is first and selected by default. The dashboard absorbs Phase 5, but
/// it is not what the app is for — the reason this exists is to say something
/// and have it captured, and burying that behind a tab you have to choose
/// would make the app slower than the Shortcut it replaced.
struct RootView: View {
    var body: some View {
        TabView {
            Tab("Talk", systemImage: "mic.fill") { TalkView() }
            Tab("Agenda", systemImage: "calendar") { AgendaView() }
            Tab("Activity", systemImage: "waveform") { ActivityView() }
            Tab("Jobs", systemImage: "gearshape.2") { JobsView() }
            Tab("Health", systemImage: "heart.text.square") { HealthView() }
        }
    }
}
