import MapKit
import SwiftUI

struct ContentView: View {
    @State private var store = LocationStore()
    @State private var camera: MapCameraPosition = .automatic
    @State private var visibleRegion: MKCoordinateRegion?
    @State private var query = ""
    @State private var search = SearchModel()
    /// Set while we write a picked result into the field, so it doesn't
    /// trigger a fresh round of suggestions.
    @State private var settingQuery = false
    @State private var showSetup = false
    @State private var showSaveBookmark = false
    @State private var bookmarkName = ""

    var body: some View {
        ZStack(alignment: .top) {
            map
            VStack(spacing: 8) {
                searchBar
                if !search.results.isEmpty { resultsList }
            }
            .padding(.horizontal, 16)
            .padding(.top, 8)
        }
        .safeAreaInset(edge: .bottom) { ControlPanel(store: store, showSetup: $showSetup, saveBookmark: {
            bookmarkName = store.pinTitle ?? ""
            showSaveBookmark = true
        }, goTo: focus) }
        .sheet(isPresented: $showSetup) {
            #if os(iOS)
            SetupView(store: store)
            #else
            MacSetupView(store: store).frame(minWidth: 520, minHeight: 560)
            #endif
        }
        .alert("Save this place", isPresented: $showSaveBookmark) {
            TextField("Name", text: $bookmarkName)
            Button("Save") { store.bookmarkPin(named: bookmarkName) }
            Button("Cancel", role: .cancel) {}
        }
        .onAppear {
            #if os(iOS)
            if !DeviceBridge.hasPairingFile { showSetup = true }
            #else
            if store.selectedUDID == nil { showSetup = true }
            #endif
        }
    }

    private var map: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            MapReader { proxy in
                Map(position: $camera) {
                    if let route = store.route {
                        MapPolyline(coordinates: route.points)
                            .stroke(.blue.opacity(0.7), lineWidth: 5)
                    }
                    if let pin = store.pin {
                        Marker(store.pinTitle ?? "Pin", systemImage: "mappin", coordinate: pin).tint(.red)
                    }
                    if let me = store.currentPosition(at: context.date) {
                        Annotation("Spoofed location", coordinate: me) {
                            Circle().fill(.purple).frame(width: 18, height: 18)
                                .overlay(Circle().stroke(.white, lineWidth: 3)).shadow(radius: 3)
                        }
                    }
                }
                .mapControls { MapCompass(); MapScaleView() }
                .onMapCameraChange { visibleRegion = $0.region }
                .onTapGesture { point in
                    if let c = proxy.convert(point, from: .local) {
                        store.dropPin(c)
                        search.clear()
                    }
                }
            }
        }
        .ignoresSafeArea()
    }

    private var searchBar: some View {
        HStack {
            Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
            TextField("Search a place or paste coordinates", text: $query)
                #if os(iOS)
                .textInputAutocapitalization(.never)
                #endif
                .autocorrectionDisabled()
                .submitLabel(.search)
                .onChange(of: query) { _, text in
                    if settingQuery { settingQuery = false } else { search.update(text, near: visibleRegion) }
                }
                .onSubmit { Task { await submit() } }
            if !query.isEmpty {
                Button { query = ""; search.clear() } label: {
                    Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                }
            }
        }
        .padding(12)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
    }

    private var resultsList: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(search.results) { item in
                    Button { Task { await pick(item) } } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(item.title).font(.body.weight(.medium))
                            Text(item.subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.vertical, 10).padding(.horizontal, 12)
                    }
                    .buttonStyle(.plain)
                    Divider()
                }
            }
        }
        .frame(maxHeight: 280)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
    }

    /// Return: coordinates go straight to a pin; anything else is geocoded as
    /// an address, and a single clear match is picked automatically.
    private func submit() async {
        if let c = Geo.parseCoordinate(query) {
            store.dropPin(c, title: String(format: "%.5f, %.5f", c.latitude, c.longitude))
            search.clear()
            return focus(c)
        }
        let found = await search.submit(query)
        if let top = found.pick {
            await pick(top)
        } else if found.choices.isEmpty {
            store.status = "No address found for “\(query)”. Try adding the city and state."
            store.isError = true
        } else {
            search.results = found.choices
            store.status = "Not sure which one you meant. Pick one, or add the city and state."
            store.isError = false
        }
    }

    private func pick(_ item: SearchResult) async {
        guard let (c, name) = await search.resolve(item) else {
            store.status = "Couldn't find “\(item.title)” on the map."
            store.isError = true
            return
        }
        settingQuery = true
        query = name
        search.clear()
        store.dropPin(c, title: name)
        focus(c)
    }

    private func focus(_ c: CLLocationCoordinate2D) {
        withAnimation {
            camera = .region(MKCoordinateRegion(center: c, latitudinalMeters: 1200, longitudinalMeters: 1200))
        }
    }
}

private struct ControlPanel: View {
    let store: LocationStore
    @Binding var showSetup: Bool
    let saveBookmark: () -> Void
    let goTo: (CLLocationCoordinate2D) -> Void

    var body: some View {
        VStack(spacing: 12) {
            HStack(alignment: .top) {
                if store.busy { ProgressView().padding(.trailing, 4) }
                Text(store.status)
                    .font(.subheadline)
                    .foregroundStyle(store.isError ? .red : .primary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
                Menu {
                    ForEach(store.bookmarks) { place in
                        Button(place.name) { store.dropPin(place.coordinate, title: place.name); goTo(place.coordinate) }
                    }
                    Divider()
                    Button("Save pin as bookmark", systemImage: "star", action: saveBookmark)
                        .disabled(store.pin == nil)
                } label: {
                    Image(systemName: "star.circle.fill").font(.title2)
                }
                Button { showSetup = true } label: { Image(systemName: "gearshape.fill").font(.title2) }
            }

            Picker("Mode", selection: Binding(get: { store.mode }, set: { store.mode = $0 })) {
                ForEach(TravelMode.allCases) { Label($0.label, systemImage: $0.symbol).tag($0) }
            }
            .pickerStyle(.segmented)

            HStack(spacing: 10) {
                action("Teleport", "location.fill", .accentColor) { await store.teleportToPin() }
                action("Go", store.mode.symbol, .green) { await store.travelToPin() }
                action("Reset", "arrow.uturn.backward", .red) { await store.reset() }
            }
        }
        .padding(16)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20))
        .padding(.horizontal, 12)
    }

    private func action(_ title: String, _ symbol: String, _ tint: Color, _ run: @escaping () async -> Void) -> some View {
        Button { Task { await run() } } label: {
            Label(title, systemImage: symbol).frame(maxWidth: .infinity).padding(.vertical, 6)
        }
        .buttonStyle(.borderedProminent)
        .tint(tint)
        .disabled(store.busy)
    }
}
