//! USB/Wi-Fi operations through macOS's usbmuxd, for the Mac app.
//!
//! Exposed to Swift as one JSON-in/JSON-out call, `apex_usb_call`:
//!   {"op":"devices"}
//!   {"op":"pair","udid":U}            -> {"state":"paired|pending|locked|denied"}
//!   {"op":"reveal_devmode","udid":U}
//!   {"op":"pairfile","udid":U,"path":OUT}  (pairing file for the iPhone app)
//!   {"op":"mount","udid":U,"dir":PATH}
//!   {"op":"set","udid":U,"lat":F,"lng":F}
//!   {"op":"play","udid":U,"points":[[lat,lng],...],"speed":F}
//!   {"op":"clear","udid":U}
//! Response: {"ok":true,...} or {"ok":false,"error":"..."}.

use std::ffi::{CStr, CString, c_char};

use idevice::{
    IdeviceError, IdeviceService,
    amfi::AmfiClient,
    lockdown::LockdownClient,
    usbmuxd::{Connection, UsbmuxdAddr, UsbmuxdConnection, UsbmuxdDevice},
    provider::UsbmuxdProvider,
};
use serde_json::{Value, json};

use crate::{Coordinate, LocationSession, ensure_ddi};

const LABEL: &str = "apex-location-director";

type Res = Result<Value, String>;

fn err(context: &str, e: IdeviceError) -> String {
    let text = format!("{e:?}");
    if text.contains("PasswordProtected") {
        return "Unlock the iPhone and keep it on the home screen.".into();
    }
    if text.contains("ServiceNotFound") || text.contains("InvalidService") {
        return "The location service isn't available yet. Make sure Developer Mode is on, then press Prepare.".into();
    }
    format!("{context}: {text}")
}

async fn mux() -> Result<UsbmuxdConnection, String> {
    UsbmuxdConnection::default().await.map_err(|e| err("usbmuxd", e))
}

/// All attached devices, one entry per UDID, preferring USB over Wi-Fi.
async fn devices(mux: &mut UsbmuxdConnection) -> Result<Vec<UsbmuxdDevice>, String> {
    let mut all = mux.get_devices().await.map_err(|e| err("list devices", e))?;
    all.sort_by_key(|d| d.connection_type != Connection::Usb);
    let mut seen = std::collections::HashSet::new();
    all.retain(|d| seen.insert(d.udid.clone()));
    Ok(all)
}

async fn provider(udid: &str) -> Result<UsbmuxdProvider, String> {
    let mut m = mux().await?;
    let device = devices(&mut m)
        .await?
        .into_iter()
        .find(|d| d.udid == udid)
        .ok_or("That iPhone is no longer connected.")?;
    let addr = UsbmuxdAddr::from_env_var().map_err(|e| format!("usbmuxd address: {e}"))?;
    Ok(device.to_provider(addr, LABEL))
}


async fn list() -> Res {
    let mut m = mux().await?;
    let addr = UsbmuxdAddr::from_env_var().map_err(|e| format!("usbmuxd address: {e}"))?;
    let mut out = Vec::new();
    for d in devices(&mut m).await? {
        let paired = m.get_pair_record(&d.udid).await.is_ok();
        let p = d.to_provider(addr.clone(), LABEL);
        let mut info = json!({
            "udid": d.udid,
            "usb": d.connection_type == Connection::Usb,
            "paired": paired,
        });
        if let Ok(mut lockdown) = LockdownClient::connect(&p).await {
            if paired {
                if let Ok(pf) = m.get_pair_record(&d.udid).await {
                    let _ = lockdown.start_session(&pf).await;
                }
            }
            for (key, name) in [("DeviceName", "name"), ("ProductVersion", "version"), ("ProductType", "model")] {
                if let Ok(v) = lockdown.get_value(Some(key), None).await {
                    info[name] = json!(v.as_string().unwrap_or_default());  // plist::Value
                }
            }
        }
        if paired {
            if let Ok(mut amfi) = AmfiClient::connect(&p).await {
                if let Ok(on) = amfi.get_developer_mode_status().await {
                    info["developerMode"] = json!(on);
                }
            }
        }
        out.push(info);
    }
    Ok(json!({ "devices": out }))
}

/// One pairing attempt. The Swift side polls this while the user answers
/// the "Trust This Computer?" prompt.
async fn pair(udid: &str) -> Res {
    let mut m = mux().await?;
    if m.get_pair_record(udid).await.is_ok() {
        return Ok(json!({ "state": "paired" }));
    }
    let p = provider(udid).await?;
    let mut lockdown = LockdownClient::connect(&p).await.map_err(|e| err("lockdown", e))?;
    let buid = m.get_buid().await.map_err(|e| err("usbmuxd BUID", e))?;
    let host_id = uuid_v4_upper();
    match lockdown.pair(host_id, buid, Some("Apex Location Director")).await {
        Ok(mut pairing_file) => {
            lockdown.start_session(&pairing_file).await.map_err(|e| err("verify pairing", e))?;
            pairing_file.udid = Some(udid.to_owned());
            let bytes = pairing_file.serialize().map_err(|e| err("serialize pairing", e))?;
            m.save_pair_record(udid, bytes).await.map_err(|e| err("save pairing", e))?;
            Ok(json!({ "state": "paired" }))
        }
        Err(e) => {
            let t = format!("{e:?}");
            let state = if t.contains("PairingDialogResponsePending") {
                "pending"
            } else if t.contains("PasswordProtected") {
                "locked"
            } else if t.contains("UserDeniedPairing") {
                "denied"
            } else {
                return Err(err("pair", e));
            };
            Ok(json!({ "state": state }))
        }
    }
}

fn uuid_v4_upper() -> String {
    // Host IDs only need to be unique per host; derive one from the clock and pid.
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let x = nanos ^ ((std::process::id() as u128) << 64);
    let h = format!("{x:032X}");
    format!("{}-{}-4{}-A{}-{}", &h[0..8], &h[8..12], &h[13..16], &h[17..20], &h[20..32])
}

async fn run(req: Value) -> Res {
    let op = req["op"].as_str().unwrap_or_default();
    if op == "devices" {
        return list().await;
    }
    let udid = req["udid"].as_str().ok_or("missing udid")?;
    match op {
        "pair" => pair(udid).await,
        "pairfile" => {
            let path = req["path"].as_str().ok_or("missing path")?;
            let mut record = mux()
                .await?
                .get_pair_record(udid)
                .await
                .map_err(|_| "Pair this iPhone first (step 1).".to_string())?;
            record.udid = Some(udid.to_owned());
            let bytes = record.serialize().map_err(|e| err("serialize pairing", e))?;
            std::fs::write(path, bytes).map_err(|e| format!("Couldn't save the file: {e}"))?;
            Ok(json!({}))
        }
        "reveal_devmode" => {
            let p = provider(udid).await?;
            let mut amfi = AmfiClient::connect(&p).await.map_err(|e| err("amfi", e))?;
            amfi.reveal_developer_mode_option_in_ui().await.map_err(|e| err("reveal Developer Mode", e))?;
            Ok(json!({}))
        }
        "mount" => {
            let dir = req["dir"].as_str().ok_or("missing dir")?;
            let p = provider(udid).await?;
            let installed = ensure_ddi(&p, std::path::Path::new(dir)).await.map_err(|e| err("developer image", e))?;
            Ok(json!({ "installed": installed }))
        }
        "set" | "play" | "clear" => {
            let p = provider(udid).await?;
            let mut s = LocationSession::connect(&p).await.map_err(|e| err("connect", e))?;
            match op {
                "set" => {
                    let c = Coordinate::new(
                        req["lat"].as_f64().ok_or("missing lat")?,
                        req["lng"].as_f64().ok_or("missing lng")?,
                    )?;
                    s.set(c).await.map_err(|e| err("set location", e))?;
                }
                "play" => {
                    let speed = req["speed"].as_f64().filter(|v| *v > 0.0).ok_or("speed must be positive")?;
                    let points = req["points"]
                        .as_array()
                        .ok_or("missing points")?
                        .iter()
                        .map(|p| {
                            Coordinate::new(
                                p[0].as_f64().ok_or("bad point")?,
                                p[1].as_f64().ok_or("bad point")?,
                            )
                        })
                        .collect::<Result<Vec<_>, _>>()?;
                    if points.is_empty() {
                        return Err("route has no points".into());
                    }
                    s.play(&points, speed).await.map_err(|e| err("route", e))?;
                }
                _ => s.clear().await.map_err(|e| err("reset", e))?,
            }
            Ok(json!({}))
        }
        other => Err(format!("unknown op {other}")),
    }
}

/// Async JSON-in/JSON-out entry point for native callers (e.g. the Tauri
/// desktop app). Always returns a JSON object with `ok: bool`; on failure it
/// also carries `error`. Each call is bounded so a stuck phone can't hang.
pub async fn handle(req: Value) -> Value {
    // Run on a dedicated thread with its own current-thread runtime. The device
    // work (idevice) need not be `Send`; the Tauri command only awaits the
    // oneshot below, whose future IS `Send`.
    let (tx, rx) = tokio::sync::oneshot::channel::<Res>();
    std::thread::spawn(move || {
        let out = match tokio::runtime::Builder::new_current_thread().enable_all().build() {
            Ok(rt) => rt.block_on(async {
                match tokio::time::timeout(std::time::Duration::from_secs(200), run(req)).await {
                    Ok(r) => r,
                    Err(_) => Err("The iPhone stopped responding. Unplug it, plug it back in and try again.".to_string()),
                }
            }),
            Err(e) => Err(format!("runtime: {e}")),
        };
        let _ = tx.send(out);
    });
    let result = rx.await.unwrap_or_else(|_| Err("internal error: worker thread died".to_string()));
    match result {
        Ok(mut v) => {
            v["ok"] = json!(true);
            v
        }
        Err(e) => json!({ "ok": false, "error": e }),
    }
}

/// JSON request in, JSON response out. Blocking. Free the result with
/// `apex_free_string`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn apex_usb_call(request: *const c_char) -> *mut c_char {
    let response = (|| -> Res {
        if request.is_null() {
            return Err("null request".into());
        }
        let text = unsafe { CStr::from_ptr(request) }.to_str().map_err(|_| "request is not UTF-8")?;
        let req: Value = serde_json::from_str(text).map_err(|e| format!("bad request: {e}"))?;
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|e| format!("runtime: {e}"))?;
        // Bound every call so a phone that stops answering can't hang the app.
        rt.block_on(async {
            tokio::time::timeout(std::time::Duration::from_secs(200), run(req))
                .await
                .map_err(|_| "The iPhone stopped responding. Unplug it, plug it back in and try again.".to_string())?
        })
    })();
    let body = match response {
        Ok(mut v) => {
            v["ok"] = json!(true);
            v
        }
        Err(e) => json!({ "ok": false, "error": e }),
    };
    CString::new(body.to_string()).unwrap_or_default().into_raw()
}
