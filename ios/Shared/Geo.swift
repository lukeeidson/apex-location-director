import CoreLocation
import MapKit

enum Geo {
    /// Parses "29.48226, -95.50940", "29.48226 N, 95.50940 W" and similar.
    static func parseCoordinate(_ text: String) -> CLLocationCoordinate2D? {
        let cleaned = text.uppercased()
            .replacingOccurrences(of: "°", with: " ")
            .replacingOccurrences(of: ",", with: " ")
        let tokens = cleaned.split(whereSeparator: \.isWhitespace).map(String.init)
        var values: [Double] = []
        for token in tokens {
            if let hemi = token.last, "NSEW".contains(hemi) {
                let number = token.dropLast()
                if !number.isEmpty {
                    guard let v = Double(number) else { return nil }
                    values.append(v)
                }
                guard !values.isEmpty else { return nil }
                if hemi == "S" || hemi == "W" { values[values.count - 1] = -abs(values[values.count - 1]) }
            } else if let v = Double(token) {
                values.append(v)
            } else {
                return nil
            }
        }
        guard values.count == 2 else { return nil }
        let c = CLLocationCoordinate2D(latitude: values[0], longitude: values[1])
        return CLLocationCoordinate2DIsValid(c) ? c : nil
    }

    static func distance(_ a: CLLocationCoordinate2D, _ b: CLLocationCoordinate2D) -> CLLocationDistance {
        CLLocation(latitude: a.latitude, longitude: a.longitude)
            .distance(from: CLLocation(latitude: b.latitude, longitude: b.longitude))
    }

    static func coordinates(of polyline: MKPolyline) -> [CLLocationCoordinate2D] {
        var coords = [CLLocationCoordinate2D](repeating: .init(), count: polyline.pointCount)
        polyline.getCoordinates(&coords, range: NSRange(location: 0, length: polyline.pointCount))
        return coords
    }

    /// Drop points closer than `minSpacing` metres so long drives stay a
    /// reasonable size; endpoints are always kept.
    static func thin(_ points: [CLLocationCoordinate2D], minSpacing: CLLocationDistance) -> [CLLocationCoordinate2D] {
        guard let first = points.first, points.count > 2 else { return points }
        var out = [first]
        for p in points.dropFirst().dropLast() where distance(out[out.count - 1], p) >= minSpacing {
            out.append(p)
        }
        out.append(points[points.count - 1])
        return out
    }

    /// Where the device should be `elapsed` seconds into a route at `speed`.
    static func position(along points: [CLLocationCoordinate2D], speed: Double, elapsed: TimeInterval) -> CLLocationCoordinate2D? {
        guard var previous = points.first else { return nil }
        var remaining = max(0, elapsed) * speed
        for p in points.dropFirst() {
            let leg = distance(previous, p)
            if remaining <= leg, leg > 0 {
                let f = remaining / leg
                return .init(latitude: previous.latitude + (p.latitude - previous.latitude) * f,
                             longitude: previous.longitude + (p.longitude - previous.longitude) * f)
            }
            remaining -= leg
            previous = p
        }
        return points.last
    }

    static func length(of points: [CLLocationCoordinate2D]) -> CLLocationDistance {
        zip(points, points.dropFirst()).reduce(0) { $0 + distance($1.0, $1.1) }
    }
}
