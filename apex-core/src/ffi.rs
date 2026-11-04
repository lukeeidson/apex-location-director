//! C ABI for the iOS app (see include/apex_core.h).
//!
//! Every call is blocking and self-contained: it opens a connection to the
//! device at `ip` (10.7.0.1 through LocalDevVPN when running on the phone),
//! does its work and returns. Results: NULL on success, otherwise an error
//! message the caller must release with `apex_free_string`.

use std::ffi::{CStr, CString, c_char};
use std::future::Future;
use std::net::SocketAddr;
use std::path::Path;
use std::time::Duration;

use idevice::{pairing_file::PairingFile, provider::TcpProvider};

use crate::{Coordinate, LocationSession, ensure_ddi};

fn runtime() -> Result<tokio::runtime::Runtime, String> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|e| format!("runtime: {e}"))
}

unsafe fn text(ptr: *const c_char, what: &str) -> Result<String, String> {
    if ptr.is_null() {
        return Err(format!("{what} is null"));
    }
    unsafe { CStr::from_ptr(ptr) }
        .to_str()
        .map(str::to_owned)
        .map_err(|_| format!("{what} is not UTF-8"))
}

unsafe fn provider(pairing_path: *const c_char, ip: *const c_char) -> Result<TcpProvider, String> {
    let pairing_path = unsafe { text(pairing_path, "pairing file path") }?;
    let ip = unsafe { text(ip, "device IP") }?;
    let pairing_file = PairingFile::read_from_file(&pairing_path)
        .map_err(|e| format!("Couldn't read the pairing file: {e:?}"))?;
    Ok(TcpProvider {
        addr: ip.parse().map_err(|_| format!("Bad device IP {ip}"))?,
        scope_id: None,
        pairing_file,
        label: "apex-location-director".into(),
    })
}

/// lockdownd's port. If it doesn't answer quickly, nothing else will.
const LOCKDOWN_PORT: u16 = 62078;

/// Fail fast when the phone isn't reachable (typically: no loopback VPN),
/// then bound the whole operation so the app never waits forever.
fn run(provider: &TcpProvider, seconds: u64, op: impl Future<Output = Result<(), String>>) -> Result<(), String> {
    runtime()?.block_on(async {
        let addr = SocketAddr::new(provider.addr, LOCKDOWN_PORT);
        match tokio::time::timeout(Duration::from_secs(5), tokio::net::TcpStream::connect(addr)).await {
            Ok(Ok(_)) => {}
            _ => {
                return Err(format!(
                    "Can't reach the phone's developer services at {}. Turn on LocalDevVPN (or your loopback VPN) and make sure Wi-Fi is on.",
                    provider.addr
                ));
            }
        }
        tokio::time::timeout(Duration::from_secs(seconds), op)
            .await
            .map_err(|_| format!("The phone stopped responding (gave up after {seconds}s)."))?
    })
}

fn finish(result: Result<(), String>) -> *mut c_char {
    match result {
        Ok(()) => std::ptr::null_mut(),
        Err(msg) => CString::new(msg.replace('\0', " ")).unwrap_or_default().into_raw(),
    }
}

fn connect_error(e: idevice::IdeviceError) -> String {
    format!(
        "Couldn't reach the phone's developer services ({e:?}). Check that LocalDevVPN is \
         connected, Wi-Fi or Airplane Mode is on, and Developer Mode is enabled."
    )
}

/// Pin the location. Persists until cleared or reboot.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn apex_set(
    pairing_path: *const c_char,
    ip: *const c_char,
    latitude: f64,
    longitude: f64,
) -> *mut c_char {
    finish((|| {
        let provider = unsafe { provider(pairing_path, ip) }?;
        let at = Coordinate::new(latitude, longitude)?;
        run(&provider, 30, async {
            let mut s = LocationSession::connect(&provider).await.map_err(connect_error)?;
            s.set(at).await.map_err(|e| format!("Set location failed: {e:?}"))
        })
    })())
}

/// Have the phone travel through `count` waypoints at `speed_mps`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn apex_play(
    pairing_path: *const c_char,
    ip: *const c_char,
    latitudes: *const f64,
    longitudes: *const f64,
    count: usize,
    speed_mps: f64,
) -> *mut c_char {
    finish((|| {
        let provider = unsafe { provider(pairing_path, ip) }?;
        if count == 0 || latitudes.is_null() || longitudes.is_null() {
            return Err("route has no waypoints".into());
        }
        if !(speed_mps.is_finite() && speed_mps > 0.0) {
            return Err("speed must be positive".into());
        }
        let lats = unsafe { std::slice::from_raw_parts(latitudes, count) };
        let lngs = unsafe { std::slice::from_raw_parts(longitudes, count) };
        let points = lats
            .iter()
            .zip(lngs)
            .map(|(&la, &lo)| Coordinate::new(la, lo))
            .collect::<Result<Vec<_>, _>>()?;
        run(&provider, 30, async {
            let mut s = LocationSession::connect(&provider).await.map_err(connect_error)?;
            s.play(&points, speed_mps).await.map_err(|e| format!("Route failed: {e:?}"))
        })
    })())
}

/// Return to real GPS.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn apex_clear(pairing_path: *const c_char, ip: *const c_char) -> *mut c_char {
    finish((|| {
        let provider = unsafe { provider(pairing_path, ip) }?;
        run(&provider, 30, async {
            let mut s = LocationSession::connect(&provider).await.map_err(connect_error)?;
            s.clear().await.map_err(|e| format!("Reset failed: {e:?}"))
        })
    })())
}

/// Install the DeveloperDiskImage cryptex if missing (needed after every
/// reboot). `restore_dir` is the DDI Restore folder bundled with the app.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn apex_mount(
    pairing_path: *const c_char,
    ip: *const c_char,
    restore_dir: *const c_char,
) -> *mut c_char {
    finish((|| {
        let provider = unsafe { provider(pairing_path, ip) }?;
        let dir = unsafe { text(restore_dir, "image folder") }?;
        run(&provider, 180, async {
            ensure_ddi(&provider, Path::new(&dir))
                .await
                .map(|_| ())
                .map_err(|e| format!("Developer image install failed: {e:?}"))
        })
    })())
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn apex_free_string(s: *mut c_char) {
    if !s.is_null() {
        drop(unsafe { CString::from_raw(s) });
    }
}
