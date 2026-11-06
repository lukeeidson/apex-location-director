import AppKit
import SwiftUI

/// Pick an iPhone and walk it through the one-time setup.
struct MacSetupView: View {
    let store: LocationStore
    @Environment(\.dismiss) private var dismiss
    @State private var phones: [PhoneInfo] = []
    @State private var selected: String?
    @State private var message = "Plug in an iPhone with a cable and unlock it."
    @State private var isError = false
    @State private var working = false
    @State private var prepared = false

    private var phone: PhoneInfo? { phones.first { $0.udid == selected } }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Connect an iPhone").font(.title2.bold())

            if phones.isEmpty {
                Label("No iPhone found yet. Plug one in with a cable and unlock it.", systemImage: "cable.connector")
                    .foregroundStyle(.secondary)
            } else {
                Picker("iPhone", selection: $selected) {
                    ForEach(phones) { p in
                        Text("\(p.name) · iOS \(p.version) · \(p.usb ? "USB" : "Wi‑Fi")").tag(Optional(p.udid))
                    }
                }
            }

            if let phone {
                if phone.majorVersion < 27 {
                    Label("Tested on iOS 27. iOS \(phone.version) may not work.", systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.orange)
                }
                step(1, "Trust this Mac", done: phone.paired,
                     detail: "Unlock the iPhone. When it asks “Trust This Computer?”, tap Trust and enter its passcode.") {
                    Button("Pair") { Task { await pair(phone.udid) } }
                }
                step(2, "Turn on Developer Mode", done: phone.developerMode == true,
                     detail: "Click the button, then on the iPhone open Settings → Privacy & Security → Developer Mode, switch it on and restart. After it restarts, unlock it and tap Turn On.") {
                    Button("Show the switch on the iPhone") { Task { await reveal(phone.udid) } }
                }
                .disabled(!phone.paired)
                step(3, "Prepare the iPhone", done: prepared,
                     detail: "Installs Apple's developer image. Needs internet. Repeat after the iPhone restarts.") {
                    Button("Prepare") { Task { await prepare(phone.udid) } }
                }
                .disabled(!phone.paired)
            }

            if let phone, phone.paired {
                Divider()
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: "iphone.and.arrow.forward").font(.title2).foregroundStyle(Color.accentColor)
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Using Apex on the iPhone itself?").font(.headline)
                        Text("Save this phone's pairing file, AirDrop it to the phone, and import it in Apex's Setup. Keep it private: it lets a device control this phone's developer services.")
                            .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        Button("Export pairing file…") { Task { await exportPairing(phone) } }.disabled(working)
                    }
                }
            }

            Spacer()
            HStack {
                if working { ProgressView().controlSize(.small) }
                Text(message).foregroundStyle(isError ? .red : .secondary).fixedSize(horizontal: false, vertical: true)
                Spacer()
                Button("Done") {
                    store.selectedUDID = selected
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(selected == nil || !prepared)
            }
        }
        .padding(24)
        .task { await watchDevices() }
    }

    private func step(_ n: Int, _ title: String, done: Bool, detail: String,
                      @ViewBuilder action: () -> some View) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: done ? "checkmark.circle.fill" : "\(n).circle")
                .font(.title2).foregroundStyle(done ? .green : .accentColor)
            VStack(alignment: .leading, spacing: 6) {
                Text(title).font(.headline)
                Text(detail).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if !done { action().disabled(working) }
            }
        }
    }

    /// Refresh the phone list every couple of seconds while setup is open.
    private func watchDevices() async {
        selected = store.selectedUDID
        while !Task.isCancelled {
            if !working, let list = try? await USBBridge.devices() {
                phones = list
                if selected == nil || !list.contains(where: { $0.udid == selected }) { selected = list.first?.udid }
            }
            try? await Task.sleep(for: .seconds(2))
        }
    }

    private func run(_ text: String, _ body: () async throws -> String) async {
        working = true
        isError = false
        message = text
        do { message = try await body() } catch { message = error.localizedDescription; isError = true }
        working = false
    }

    private func pair(_ udid: String) async {
        await run("Waiting for you to tap Trust on the iPhone…") {
            for _ in 0..<90 {
                switch try await USBBridge.pair(udid) {
                case "paired": return "Paired."
                case "denied": throw BackendError(message: "Trust was declined. Unplug, plug back in and try again.")
                case "locked": message = "Unlock the iPhone to continue…"
                default: message = "Tap Trust on the iPhone and enter its passcode…"
                }
                try await Task.sleep(for: .seconds(2))
            }
            throw BackendError(message: "Timed out waiting for Trust.")
        }
    }

    private func exportPairing(_ phone: PhoneInfo) async {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "\(phone.name) pairing.plist"
        panel.allowedContentTypes = [.propertyList]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        await run("Saving pairing file…") {
            try await USBBridge.exportPairingFile(phone.udid, to: url)
            return "Saved. AirDrop it to the iPhone and import it in Apex → Setup."
        }
    }

    private func reveal(_ udid: String) async {
        await run("Asking the iPhone to show the Developer Mode switch…") {
            try await USBBridge.revealDeveloperMode(udid)
            return "Now switch on Developer Mode on the iPhone (Settings → Privacy & Security)."
        }
    }

    private func prepare(_ udid: String) async {
        await run("Preparing the iPhone (this can take a minute)…") {
            try await USBBridge(udid: udid).mountDeveloperImage()
            prepared = true
            return "Ready. Click Done."
        }
    }
}
