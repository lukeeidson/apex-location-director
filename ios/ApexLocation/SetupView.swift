import SwiftUI
import UniformTypeIdentifiers

struct SetupView: View {
    let store: LocationStore
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    @State private var importing = false
    @State private var hasPairing = DeviceBridge.hasPairingFile
    @State private var importError: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("Apex changes this iPhone's location by talking to its own developer services. That needs three things, set up once.")
                        .font(.callout)
                }

                Section("1 · LocalDevVPN") {
                    Text("Install LocalDevVPN and turn it on. Keep Wi‑Fi (or Airplane Mode) on while using Apex.")
                    Button("Get LocalDevVPN from the App Store") {
                        openURL(URL(string: "https://apps.apple.com/app/id6755608044")!)
                    }
                    LabeledContent("Device address") {
                        TextField(DeviceBridge.defaultIP, text: Binding(get: { store.deviceIP }, set: { store.deviceIP = $0 }))
                            .multilineTextAlignment(.trailing)
                            .keyboardType(.numbersAndPunctuation)
                            .autocorrectionDisabled()
                    }
                    Text("Leave this at \(DeviceBridge.defaultIP) unless you're testing another connection.")
                        .font(.caption).foregroundStyle(.secondary)
                }

                Section("2 · Pairing file") {
                    Label(hasPairing ? "Pairing file installed" : "No pairing file yet",
                          systemImage: hasPairing ? "checkmark.seal.fill" : "exclamationmark.triangle.fill")
                        .foregroundStyle(hasPairing ? .green : .orange)
                    Text("Make it once on a Mac: open the Apex Location Director Mac app, set up this iPhone, click “Export pairing file…”, AirDrop it here, then import it.")
                        .font(.caption)
                    Button(hasPairing ? "Replace pairing file" : "Import pairing file") { importing = true }
                    if let importError { Text(importError).foregroundStyle(.red).font(.caption) }
                }

                Section("3 · Developer image") {
                    Text("Needed after every restart of the phone. Requires an internet connection the first time after each restart.")
                        .font(.caption)
                    Button("Prepare developer image") { Task { await store.mountDeveloperImage() } }
                        .disabled(!hasPairing || store.busy)
                    Text(store.status).font(.caption).foregroundStyle(store.isError ? .red : .secondary)
                }

                Section("Bookmarks") {
                    ForEach(store.bookmarks) { Text($0.name) }
                        .onDelete { store.bookmarks.remove(atOffsets: $0) }
                }
            }
            .navigationTitle("Setup")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .fileImporter(isPresented: $importing, allowedContentTypes: [.propertyList, .xml, .data]) { result in
                do {
                    try DeviceBridge.importPairingFile(from: result.get())
                    hasPairing = true
                    importError = nil
                } catch {
                    importError = error.localizedDescription
                }
            }
        }
    }
}
