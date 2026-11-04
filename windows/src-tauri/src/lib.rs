//! Tauri backend for Apex Location Director (Windows/desktop).
//!
//! Thin bridge over `apex-core`: each command builds a JSON request and calls
//! `apex_core::usb::handle`, which talks to the iPhone over usbmuxd (Apple's
//! "Apple Devices"/iTunes service on Windows, built-in on macOS). The bundled
//! developer image is resolved from the app's resource directory.

use serde_json::{Value, json};
use tauri::{AppHandle, Manager};

/// Locate the bundled DeveloperDiskImage `Restore` directory.
fn ddi_dir(app: &AppHandle) -> Result<String, String> {
    let dir = app
        .path()
        .resource_dir()
        .map_err(|e| format!("resource dir: {e}"))?
        .join("DDI");
    dir.to_str()
        .map(str::to_owned)
        .ok_or_else(|| "developer image path is not valid UTF-8".to_string())
}

#[tauri::command]
async fn devices() -> Value {
    apex_core::usb::handle(json!({ "op": "devices" })).await
}

#[tauri::command]
async fn pair(udid: String) -> Value {
    apex_core::usb::handle(json!({ "op": "pair", "udid": udid })).await
}

#[tauri::command]
async fn reveal_dev_mode(udid: String) -> Value {
    apex_core::usb::handle(json!({ "op": "reveal_devmode", "udid": udid })).await
}

#[tauri::command]
async fn prepare(app: AppHandle, udid: String) -> Value {
    match ddi_dir(&app) {
        Ok(dir) => apex_core::usb::handle(json!({ "op": "mount", "udid": udid, "dir": dir })).await,
        Err(e) => json!({ "ok": false, "error": e }),
    }
}

#[tauri::command]
async fn set_location(udid: String, lat: f64, lng: f64) -> Value {
    apex_core::usb::handle(json!({ "op": "set", "udid": udid, "lat": lat, "lng": lng })).await
}

#[tauri::command]
async fn play_route(udid: String, points: Vec<[f64; 2]>, speed: f64) -> Value {
    apex_core::usb::handle(json!({ "op": "play", "udid": udid, "points": points, "speed": speed })).await
}

#[tauri::command]
async fn clear(udid: String) -> Value {
    apex_core::usb::handle(json!({ "op": "clear", "udid": udid })).await
}

/// Check a license key against the embedded public key. Offline, instant.
#[tauri::command]
fn verify_license(key: String) -> bool {
    apex_core::license::verify(&key)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            devices,
            pair,
            reveal_dev_mode,
            prepare,
            set_location,
            play_route,
            clear,
            verify_license,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
