import AppKit
import SwiftUI

@main
struct AmneziaMonitorApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = MonitorModel()

    var body: some Scene {
        WindowGroup("Amnezia Monitor", id: "main") {
            RootView(model: model)
        }
        .defaultSize(width: 1080, height: 740)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }

        MenuBarExtra {
            MenuBarView(model: model)
        } label: {
            MenuBarLabel(symbol: model.menuSymbol, title: model.menuTitle)
        }
        .menuBarExtraStyle(.window)
    }
}

/// Значок в строке меню. Появляется сразу, поэтому здесь же запоминаем, как открыть окно.
private struct MenuBarLabel: View {
    var symbol: String
    var title: String
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: symbol)
            if !title.isEmpty {
                Text(title)
            }
        }
        .onAppear {
            AppDelegate.openMain = { openWindow(id: "main") }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    /// Создать окно WindowGroup, если его закрыли. Заполняется из строки меню.
    static var openMain: (() -> Void)?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        Self.activate()
        // WindowGroup создаёт окно сам. Если к этому моменту его ещё нет — открываем.
        DispatchQueue.main.async {
            Self.revealMainWindow(createIfMissing: true)
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            Self.revealMainWindow(createIfMissing: true)
        }
        Self.activate()
        return true
    }

    static func revealMainWindow(createIfMissing: Bool) {
        activate()
        if let window = NSApp.windows.first(where: { $0.canBecomeMain }) {
            if window.isMiniaturized {
                window.deminiaturize(nil)
            }
            window.makeKeyAndOrderFront(nil)
            return
        }
        if createIfMissing {
            openMain?()
        }
    }

    static func activate() {
        if #available(macOS 14.0, *) {
            NSApp.activate()
        } else {
            NSApp.activate(ignoringOtherApps: true)
        }
    }
}

enum AppActivate {
    static func front() {
        AppDelegate.activate()
    }
}
