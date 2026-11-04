import CoreLocation
import Foundation

/// An iPhone seen by the Mac through usbmuxd (USB, or Wi-Fi once paired).
struct PhoneInfo: Identifiable, Hashable {
    let udid: String
    let name: String
    let model: String
    let version: String
    let usb: Bool
    let paired: Bool
    /// nil when the phone didn't say (locked, or only reachable over Wi-Fi).
    let developerMode: Bool?

    var id: String { udid }
    var majorVersion: Int { Int(version.split(separator: ".").first ?? "") ?? 0 }
}

/// Mac backend: drives a phone over USB/Wi-Fi through the Rust core's JSON
/// bridge (apex-core/src/usb.rs).
struct USBBridge: LocationBackend {
    let udid: String

    func set(_ c: CLLocationCoordinate2D) async throws {
        _ = try await Self.call(["op": "set", "udid": udid, "lat": c.latitude, "lng": c.longitude])
    }

    func play(_ waypoints: [CLLocationCoordinate2D], speed: Double) async throws {
        let points = waypoints.map { [$0.latitude, $0.longitude] }
        _ = try await Self.call(["op": "play", "udid": udid, "points": points, "speed": speed])
    }

    func clear() async throws {
        _ = try await Self.call(["op": "clear", "udid": udid])
    }

    func mountDeveloperImage() async throws {
        guard let dir = Bundle.main.url(forResource: "DDI", withExtension: nil) else {
            throw BackendError(message: "The developer image is missing from the app.")
        }
        _ = try await Self.call(["op": "mount", "udid": udid, "dir": dir.path])
    }

    static func devices() async throws -> [PhoneInfo] {
        let out = try await call(["op": "devices"])
        return (out["devices"] as? [[String: Any]] ?? []).map {
            PhoneInfo(udid: $0["udid"] as? String ?? "",
                      name: $0["name"] as? String ?? "iPhone",
                      model: $0["model"] as? String ?? "",
                      version: $0["version"] as? String ?? "?",
                      usb: $0["usb"] as? Bool ?? false,
                      paired: $0["paired"] as? Bool ?? false,
                      developerMode: $0["developerMode"] as? Bool)
        }
    }

    /// One pairing attempt: "paired", "pending" (Trust prompt showing),
    /// "locked" or "denied".
    static func pair(_ udid: String) async throws -> String {
        try await call(["op": "pair", "udid": udid])["state"] as? String ?? "pending"
    }

    /// Save the pairing file the iPhone app needs to talk to this phone.
    static func exportPairingFile(_ udid: String, to url: URL) async throws {
        _ = try await call(["op": "pairfile", "udid": udid, "path": url.path])
    }

    static func revealDeveloperMode(_ udid: String) async throws {
        _ = try await call(["op": "reveal_devmode", "udid": udid])
    }

    private static func call(_ request: [String: Any]) async throws -> [String: Any] {
        let body = String(decoding: try JSONSerialization.data(withJSONObject: request), as: UTF8.self)
        // Blocking device I/O: keep it off the main actor.
        let text: String = await Task.detached(priority: .userInitiated) {
            body.withCString { ptr in
                guard let raw = apex_usb_call(ptr) else { return #"{"ok":false,"error":"no response"}"# }
                defer { apex_free_string(raw) }
                return String(cString: raw)
            }
        }.value
        let json = (try? JSONSerialization.jsonObject(with: Data(text.utf8))) as? [String: Any] ?? [:]
        guard json["ok"] as? Bool == true else {
            throw BackendError(message: json["error"] as? String ?? "Unknown error talking to the iPhone.")
        }
        return json
    }
}
