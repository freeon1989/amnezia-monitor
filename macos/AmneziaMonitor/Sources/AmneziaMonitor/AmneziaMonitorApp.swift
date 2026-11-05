import AppKit
import SwiftUI

@main
struct AmneziaMonitorApp: App {
    @StateObject private var model = MonitorModel()

    var body: some Scene {
        MenuBarExtra {
            MenuBarView(model: model)
        } label: {
            HStack(spacing: 4) {
                Image(systemName: model.menuSymbol)
                if !model.menuTitle.isEmpty {
                    Text(model.menuTitle)
                }
            }
        }
        .menuBarExtraStyle(.window)

        Window("Amnezia Monitor", id: "main") {
            RootView(model: model)
        }
        .defaultSize(width: 1080, height: 740)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}

enum AppActivate {
    static func front() {
        if #available(macOS 14.0, *) {
            NSApp.activate()
        } else {
            NSApp.activate(ignoringOtherApps: true)
        }
    }
}
