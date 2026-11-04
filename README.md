# Apex Location Director

Sets a persistent simulated GPS location on a connected iPhone (iOS 17+, tested on 27).
The location stays after you unplug, until cleared or the phone reboots.

- `apex-core/` — cross-platform Rust engine (uses the `idevice` crate; talks to the
  iPhone over usbmuxd). Builds a CLI, a C FFI for the iOS/macOS apps, and a JSON bridge.
- `windows/` — Tauri desktop app (Rust backend + web-map frontend) for Windows and macOS.
- `ios/` — SwiftUI iPhone app and native macOS app.

## Windows build

CI (`.github/workflows/windows.yml`) builds the Windows installer on every push to `main`.
Download it from the run's **Artifacts** (`ApexLocationDirector-Windows`).

Prerequisite on the Windows PC: install **Apple Devices** (or iTunes) so Windows can see the iPhone over USB.
