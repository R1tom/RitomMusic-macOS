import SwiftUI
import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ n: Notification) {
        MainActor.assumeIsolated {
            Pref.register()
            Downloader.shared.requestNotifications()
            Library.shared.scan()
            _ = IPod.shared
        }
    }

    // keep downloading with the window closed; the Dock icon / menu bar brings it back
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ s: NSApplication) -> NSApplication.TerminateReply {
        let (dl, pod) = MainActor.assumeIsolated { (Downloader.shared.isBusy, IPod.shared.runner.running) }
        if dl || pod {
            let a = NSAlert()
            a.messageText = pod ? "The iPod is still being written to" : "Songs are still downloading"
            a.informativeText = pod
                ? "Quitting now stops the copy. Songs already copied are kept — run the same add again to finish."
                : "Quitting stops the download. Finished songs are kept and skipped next time."
            a.addButton(withTitle: "Quit"); a.addButton(withTitle: "Cancel")
            if a.runModal() != .alertFirstButtonReturn { return .terminateCancel }
            MainActor.assumeIsolated {
                Downloader.shared.stopAll()
                IPod.shared.runner.stop()
            }
            Thread.sleep(forTimeInterval: 1.0)
        }
        return .terminateNow
    }
}

@main
struct RitomMusicApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate

    var body: some Scene {
        Window("Ritom Music", id: "main") {
            ContentView().frame(minWidth: 1120, minHeight: 660)
        }
        .defaultSize(width: 1280, height: 820)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandMenu("iPod") {
                Button("Safely Eject iPod") { IPod.shared.safeEject() }.keyboardShortcut("e", modifiers: .command)
            }
        }
        Settings { SettingsView() }
        MenuBarExtra {
            MenuBarContent()
        } label: {
            MenuBarIcon()
        }
    }
}

struct MenuBarContent: View {
    @ObservedObject var dl = Downloader.shared
    @ObservedObject var pod = IPod.shared
    @Environment(\.openWindow) private var openWindow
    var body: some View {
        if dl.isBusy {
            Text(dl.total > 0 ? "Downloading \(dl.doneCount) of \(dl.total)" : "Downloading…")
            Text(dl.phase)
            Button("Stop Download") { dl.stop() }
        } else {
            Text("Nothing downloading")
        }
        if let v = pod.volume {
            Divider()
            Text("iPod: \(URL(fileURLWithPath: v).lastPathComponent)\(pod.runner.running ? " (busy)" : "")")
            if pod.ejectState == .waiting {
                Button("Cancel Eject (waiting for copy to finish)") { pod.cancelEject() }
            } else {
                Button(pod.runner.running ? "Safely Eject iPod When Done" : "Safely Eject iPod") { pod.safeEject() }
            }
        }
        Divider()
        Button("Download Link from Clipboard") {
            if let s = NSPasteboard.general.string(forType: .string) {
                _ = dl.enqueue(text: s, options: JobOptions())
            }
            openWindow(id: "main"); NSApp.activate(ignoringOtherApps: true)
        }
        Button("Open Ritom Music") { openWindow(id: "main"); NSApp.activate(ignoringOtherApps: true) }
        Button("Open Music Folder") { NSWorkspace.shared.open(URL(fileURLWithPath: Pref.out)) }
        Divider()
        Button("Quit") { NSApp.terminate(nil) }.keyboardShortcut("q")
    }
}

struct MenuBarIcon: View {
    @ObservedObject var runner = Downloader.shared.runner
    var body: some View { Image(systemName: runner.running ? "arrow.down.circle.fill" : "music.note") }
}
