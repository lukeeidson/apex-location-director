import CoreLocation
import MapKit
import Observation

/// One row in the search dropdown: either an Apple autocomplete suggestion
/// (resolved to a coordinate when picked) or an exact geocoded address.
struct SearchResult: Identifiable {
    let id = UUID()
    let title: String
    let subtitle: String
    fileprivate let completion: MKLocalSearchCompletion?
    fileprivate let coordinate: CLLocationCoordinate2D?
}

/// Address and place search that behaves like Apple Maps: live suggestions
/// while typing (MKLocalSearchCompleter, which understands full street
/// addresses) and exact address geocoding on Return.
@MainActor @Observable
final class SearchModel: NSObject, MKLocalSearchCompleterDelegate {
    var results: [SearchResult] = []

    @ObservationIgnored private let completer = MKLocalSearchCompleter()
    @ObservationIgnored private let geocoder = CLGeocoder()

    override init() {
        super.init()
        completer.delegate = self
        completer.resultTypes = [.address, .pointOfInterest]
    }

    /// Feed the text as the user types. The visible map region only biases
    /// suggestions; addresses elsewhere still come back.
    func update(_ text: String, near region: MKCoordinateRegion?) {
        let q = text.trimmingCharacters(in: .whitespaces)
        guard q.count >= 3, Geo.parseCoordinate(q) == nil else {
            clear()
            return
        }
        if let region { completer.region = region }
        completer.queryFragment = q
    }

    func clear() {
        completer.cancel()
        results = []
    }

    /// Return pressed. Apple's autocomplete is far better at full addresses
    /// than the plain geocoder (which happily returns a similar street in
    /// another town), so the top suggestion wins, like in Apple Maps.
    /// Returns either a row to pick right away, or rows to choose from.
    func submit(_ text: String) async -> (pick: SearchResult?, choices: [SearchResult]) {
        let q = text.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return (nil, []) }
        // Give in-flight suggestions for the latest keystroke a moment to land.
        for _ in 0..<15 where results.isEmpty || completer.queryFragment != q {
            if completer.queryFragment != q { completer.queryFragment = q }
            try? await Task.sleep(for: .milliseconds(100))
        }
        if let top = results.first { return (top, []) }

        let placemarks = (try? await geocoder.geocodeAddressString(q)) ?? []
        // Geocoder matches are only offered, never auto-picked.
        return (nil, placemarks.compactMap { p -> SearchResult? in
            guard let c = p.location?.coordinate else { return nil }
            let street = [p.subThoroughfare, p.thoroughfare].compactMap { $0 }.joined(separator: " ")
            let area = [p.locality, p.administrativeArea, p.postalCode].compactMap { $0 }.joined(separator: ", ")
            return SearchResult(title: street.isEmpty ? (p.name ?? q) : street, subtitle: area,
                                completion: nil, coordinate: c)
        })
    }

    /// Coordinate and display name for a picked row.
    func resolve(_ result: SearchResult) async -> (CLLocationCoordinate2D, String)? {
        let name = result.subtitle.isEmpty ? result.title : "\(result.title), \(result.subtitle)"
        if let c = result.coordinate { return (c, name) }
        guard let completion = result.completion else { return nil }
        let item = try? await MKLocalSearch(request: MKLocalSearch.Request(completion: completion)).start().mapItems.first
        guard let c = item?.placemark.coordinate else { return nil }
        return (c, name)
    }

    nonisolated func completerDidUpdateResults(_ completer: MKLocalSearchCompleter) {
        MainActor.assumeIsolated {
            results = completer.results.prefix(8).map {
                SearchResult(title: $0.title, subtitle: $0.subtitle, completion: $0, coordinate: nil)
            }
        }
    }

    nonisolated func completer(_ completer: MKLocalSearchCompleter, didFailWithError error: Error) {
        MainActor.assumeIsolated { results = [] }
    }
}
