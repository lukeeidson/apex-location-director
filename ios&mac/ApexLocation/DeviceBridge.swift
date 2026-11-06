import CoreLocation
import Foundation

/// Swift wrapper around the Rust core (apex-core/src/ffi.rs).
///
/// The phone talks to its own developer services: LocalDevVPN routes
/// 10.7.0.1 back to this device, and the pairing file proves we are a
/// trusted host. Every call opens a fresh connection, so nothing has to stay
/// running after it returns - the simulation lives on the device.
struct DeviceBridge: LocationBackend {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    static let defaultIP = "10.7.0.1"

    var ip: String

    static var pairingFileURL: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("pairing.plist")
    }

    static var hasPairingFile: Bool {
        FileManager.default.fileExists(atPath: pairingFileURL.path)
    }

    /// Copy a user-picked pairing file into the app's Documents folder.
    static func importPairingFile(from source: URL) throws {
        let scoped = source.startAccessingSecurityScopedResource()
        defer { if scoped { source.stopAccessingSecurityScopedResource() } }
        let data = try Data(contentsOf: source)
        guard (try? PropertyListSerialization.propertyList(from: data, format: nil)) is [String: Any] else {
            throw Failure(message: "That file isn't a pairing file (.plist).")
        }
        try data.write(to: pairingFileURL, options: [.atomic, .completeFileProtection])
    }

    func set(_ c: CLLocationCoordinate2D) async throws {
        try await run { pairing, ip in apex_set(pairing, ip, c.latitude, c.longitude) }
    }

    func play(_ waypoints: [CLLocationCoordinate2D], speed: Double) async throws {
        let lats = waypoints.map(\.latitude)
        let lngs = waypoints.map(\.longitude)
        try await run { pairing, ip in
            lats.withUnsafeBufferPointer { la in
                lngs.withUnsafeBufferPointer { lo in
                    apex_play(pairing, ip, la.baseAddress, lo.baseAddress, la.count, speed)
                }
            }
        }
    }

    func clear() async throws {
        try await run { pairing, ip in apex_clear(pairing, ip) }
    }

    /// Install the developer image if missing (required after every reboot).
    func mountDeveloperImage() async throws {
        guard let dir = Bundle.main.url(forResource: "DDI", withExtension: nil) else {
            throw Failure(message: "The developer image is missing from the app bundle.")
        }
        try await run { pairing, ip in apex_mount(pairing, ip, dir.path) }
    }

    private func run(_ body: @escaping @Sendable (UnsafePointer<CChar>, UnsafePointer<CChar>) -> UnsafeMutablePointer<CChar>?) async throws {
        guard Self.hasPairingFile else {
            throw Failure(message: "No pairing file yet. Import one in Setup.")
        }
        let pairing = Self.pairingFileURL.path
        let ip = self.ip
        // The Rust calls block on network I/O, so keep them off the main actor.
        let error: String? = await Task.detached(priority: .userInitiated) {
            pairing.withCString { p in
                ip.withCString { i in
                    guard let raw = body(p, i) else { return nil }
                    let message = String(cString: raw)
                    apex_free_string(raw)
                    return message
                }
            }
        }.value
        if let error { throw Failure(message: error) }
    }
}
