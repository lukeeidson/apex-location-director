import CoreLocation
import MapKit
import Observation

/// How the store reaches the phone: the iPhone app talks to itself through
/// LocalDevVPN (DeviceBridge), the Mac app over USB/Wi-Fi (USBBridge).
protocol LocationBackend: Sendable {
    func set(_ c: CLLocationCoordinate2D) async throws
    func play(_ waypoints: [CLLocationCoordinate2D], speed: Double) async throws
    func clear() async throws
    func mountDeveloperImage() async throws
}

struct BackendError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

struct Place: Codable, Identifiable, Hashable {
    var id = UUID()
    var name: String
    var latitude: Double
    var longitude: Double
    var coordinate: CLLocationCoordinate2D { .init(latitude: latitude, longitude: longitude) }
}

enum TravelMode: String, CaseIterable, Identifiable, Codable {
    case walk, run, cycle, drive
    var id: String { rawValue }
    var label: String { rawValue.capitalized }
    var symbol: String {
        switch self {
        case .walk: "figure.walk"
        case .run: "figure.run"
        case .cycle: "bicycle"
        case .drive: "car.fill"
        }
    }
    /// nil = use Apple's estimated driving time for the route.
    var fixedSpeed: Double? {
        switch self {
        case .walk: 1.4
        case .run: 3.0
        case .cycle: 5.5
        case .drive: nil
        }
    }
    var directionsType: MKDirectionsTransportType { self == .drive ? .automobile : .walking }
}

/// A route the phone is playing on its own.
struct ActiveRoute: Codable {
    var latitudes: [Double]
    var longitudes: [Double]
    var speed: Double
    var startedAt: Date
    var points: [CLLocationCoordinate2D] {
        zip(latitudes, longitudes).map { CLLocationCoordinate2D(latitude: $0, longitude: $1) }
    }
    var duration: TimeInterval { Geo.length(of: points) / speed }
}

@MainActor @Observable
final class LocationStore {
    var pin: CLLocationCoordinate2D?
    var pinTitle: String?
    var simulated: CLLocationCoordinate2D? { didSet { save() } }
    var route: ActiveRoute? { didSet { save() } }
    var bookmarks: [Place] = [] { didSet { save() } }
    var mode: TravelMode = .drive
    var status = "Drop a pin or search for a place."
    var isError = false
    var busy = false
    #if os(iOS)
    var deviceIP: String = UserDefaults.standard.string(forKey: "deviceIP") ?? DeviceBridge.defaultIP {
        didSet { UserDefaults.standard.set(deviceIP, forKey: "deviceIP") }
    }
    private var backend: (any LocationBackend)? { DeviceBridge(ip: deviceIP) }
    #else
    /// The iPhone the Mac app is driving.
    var selectedUDID: String?
    private var backend: (any LocationBackend)? { selectedUDID.map { USBBridge(udid: $0) } }
    #endif

    private func bridge() throws -> any LocationBackend {
        guard let backend else { throw BackendError(message: "Connect an iPhone first.") }
        return backend
    }
    private var loading = true

    init() {
        let d = UserDefaults.standard
        if let data = d.data(forKey: "bookmarks"), let b = try? JSONDecoder().decode([Place].self, from: data) {
            bookmarks = b
        } else {
            bookmarks = [
                Place(name: "Times Square, NYC", latitude: 40.7580, longitude: -73.9855),
                Place(name: "London Eye", latitude: 51.5033, longitude: -0.1195),
                Place(name: "Shibuya Crossing, Tokyo", latitude: 35.6595, longitude: 139.7004),
            ]
        }
        if let data = d.data(forKey: "route"), let r = try? JSONDecoder().decode(ActiveRoute.self, from: data),
           Date().timeIntervalSince(r.startedAt) < r.duration {
            route = r
        }
        if d.object(forKey: "simLat") != nil {
            simulated = .init(latitude: d.double(forKey: "simLat"), longitude: d.double(forKey: "simLng"))
        }
        loading = false
    }

    private func save() {
        guard !loading else { return }
        let d = UserDefaults.standard
        d.set(try? JSONEncoder().encode(bookmarks), forKey: "bookmarks")
        d.set(try? JSONEncoder().encode(route), forKey: "route")
        if let s = simulated {
            d.set(s.latitude, forKey: "simLat"); d.set(s.longitude, forKey: "simLng")
        } else {
            d.removeObject(forKey: "simLat"); d.removeObject(forKey: "simLng")
        }
    }

    /// Estimated live position while a route plays, else the pinned location.
    func currentPosition(at date: Date = .now) -> CLLocationCoordinate2D? {
        if let r = route {
            let elapsed = date.timeIntervalSince(r.startedAt)
            if elapsed < r.duration { return Geo.position(along: r.points, speed: r.speed, elapsed: elapsed) }
            return r.points.last
        }
        return simulated
    }

    // MARK: - Pin

    func dropPin(_ c: CLLocationCoordinate2D, title: String? = nil) {
        pin = c
        pinTitle = title
        status = String(format: "Pin: %.5f, %.5f", c.latitude, c.longitude)
        isError = false
    }

    // MARK: - Device actions

    func teleportToPin() async {
        guard let target = pin else { return report("Drop a pin first.", error: true) }
        await perform("Teleporting…") {
            try await self.bridge().set(target)
            self.route = nil
            self.simulated = target
            return String(format: "You're at %.5f, %.5f. It stays after you close the app.", target.latitude, target.longitude)
        }
    }

    func travelToPin() async {
        guard let target = pin else { return report("Drop a pin first.", error: true) }
        guard let start = currentPosition() else {
            return report("Teleport somewhere first; the trip starts from there.", error: true)
        }
        await perform("Finding a \(mode.label.lowercased()) route…") {
            let request = MKDirections.Request()
            request.source = MKMapItem(placemark: MKPlacemark(coordinate: start))
            request.destination = MKMapItem(placemark: MKPlacemark(coordinate: target))
            request.transportType = self.mode.directionsType
            let directions = try await MKDirections(request: request).calculate()
            guard let best = directions.routes.first else { throw BackendError(message: "No route found.") }

            let speed = self.mode.fixedSpeed ?? max(3, best.distance / max(best.expectedTravelTime, 1))
            var points = Geo.thin(Geo.coordinates(of: best.polyline), minSpacing: 15)
            if Geo.distance(points.first ?? start, start) > 1 { points.insert(start, at: 0) }

            try await self.bridge().play(points, speed: speed)
            self.route = ActiveRoute(latitudes: points.map(\.latitude), longitudes: points.map(\.longitude),
                                     speed: speed, startedAt: .now)
            self.simulated = target
            let minutes = Int((best.distance / speed / 60).rounded())
            return String(format: "%@ started: %.1f mi, about %d min at %.0f mph. Runs on its own.",
                          self.mode.label, best.distance / 1609.34, minutes, speed * 2.23694)
        }
    }

    func reset() async {
        await perform("Returning to real GPS…") {
            try await self.bridge().clear()
            self.route = nil
            self.simulated = nil
            return "Back on real GPS."
        }
    }

    func mountDeveloperImage() async {
        await perform("Checking developer image…") {
            try await self.bridge().mountDeveloperImage()
            return "Developer image ready."
        }
    }

    func bookmarkPin(named name: String) {
        guard let c = pin else { return }
        let n = name.trimmingCharacters(in: .whitespaces)
        bookmarks.append(Place(name: n.isEmpty ? (pinTitle ?? "Saved place") : n,
                               latitude: c.latitude, longitude: c.longitude))
        report("Saved “\(bookmarks.last!.name)”.")
    }

    private func perform(_ working: String, _ body: @escaping () async throws -> String) async {
        guard !busy else { return }
        busy = true
        report(working)
        defer { busy = false }
        do {
            report(try await body())
        } catch {
            report(error.localizedDescription, error: true)
        }
    }

    private func report(_ text: String, error: Bool = false) {
        status = text
        isError = error
    }
}
