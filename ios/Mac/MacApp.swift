import SwiftUI

@main
struct ApexLocationMacApp: App {
    var body: some Scene {
        WindowGroup("Apex Location Director") {
            ContentView().frame(minWidth: 820, minHeight: 620)
        }
        .windowResizability(.contentMinSize)
    }
}
