//! Mac test CLI for apex-core over USB/Wi-Fi via usbmuxd.
//!   apex set LAT LNG
//!   apex route SPEED_MPS LAT LNG LAT LNG [LAT LNG ...]
//!   apex clear
//!   apex mount [RESTORE_DIR]   (default: Xcode's iOS_DDI/Restore)
//!   apex pairfile OUT.plist    (export the pairing record for the phone app)
//!   apex json '{"op":"devices"}' (exercise the Mac app's JSON bridge)

use apex_core::{Coordinate, LocationSession, ensure_ddi};
use idevice::usbmuxd::{UsbmuxdAddr, UsbmuxdConnection};

const XCODE_DDI: &str = "/Library/Developer/DeveloperDiskImages/iOS_DDI/Restore";

fn coords(args: &[String]) -> Result<Vec<Coordinate>, String> {
    if args.is_empty() || args.len() % 2 != 0 {
        return Err("expected LAT LNG pairs".into());
    }
    args.chunks(2)
        .map(|p| {
            let lat = p[0].parse::<f64>().map_err(|e| format!("{}: {e}", p[0]))?;
            let lng = p[1].parse::<f64>().map_err(|e| format!("{}: {e}", p[1]))?;
            Coordinate::new(lat, lng)
        })
        .collect()
}

#[tokio::main]
async fn main() -> Result<(), String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(cmd) = args.first() else {
        return Err("usage: apex set LAT LNG | route SPEED LAT LNG LAT LNG... | clear | mount [DIR] | pairfile OUT".into());
    };

    if cmd == "json" {
        let req = std::ffi::CString::new(args.get(1).cloned().unwrap_or_default()).map_err(|e| e.to_string())?;
        // apex_usb_call builds its own runtime, so run it off this one.
        let out = tokio::task::spawn_blocking(move || unsafe {
            let raw = apex_core::usb::apex_usb_call(req.as_ptr());
            let s = std::ffi::CStr::from_ptr(raw).to_string_lossy().into_owned();
            apex_core::ffi::apex_free_string(raw);
            s
        })
        .await
        .map_err(|e| e.to_string())?;
        println!("{out}");
        return Ok(());
    }

    let mut mux = UsbmuxdConnection::default().await.map_err(|e| format!("usbmuxd: {e:?}"))?;
    let device = mux
        .get_devices()
        .await
        .map_err(|e| format!("list devices: {e:?}"))?
        .into_iter()
        .next()
        .ok_or("no device connected")?;
    let addr = UsbmuxdAddr::from_env_var().map_err(|e| format!("{e:?}"))?;
    let provider = device.to_provider(addr, "apex-location-director");

    if cmd == "pairfile" {
        let out = args.get(1).ok_or("usage: apex pairfile OUT.plist")?;
        let record = mux
            .get_pair_record(&device.udid)
            .await
            .map_err(|e| format!("pair record: {e:?} (pair the phone first)"))?;
        let bytes = record.serialize().map_err(|e| format!("serialize: {e:?}"))?;
        std::fs::write(out, bytes).map_err(|e| format!("write {out}: {e}"))?;
        println!("pairing file for {} written to {out}", device.udid);
        return Ok(());
    }

    if cmd == "mount" {
        let dir = args.get(1).map(String::as_str).unwrap_or(XCODE_DDI);
        let installed = ensure_ddi(&provider, std::path::Path::new(dir))
            .await
            .map_err(|e| format!("mount: {e:?}"))?;
        println!("{}", if installed { "developer image installed" } else { "developer image already installed" });
        return Ok(());
    }

    let mut session = LocationSession::connect(&provider).await.map_err(|e| format!("connect: {e:?}"))?;
    match cmd.as_str() {
        "set" => {
            let c = coords(&args[1..])?;
            session.set(c[0]).await.map_err(|e| format!("set: {e:?}"))?;
            println!("set {:.6}, {:.6}", c[0].latitude, c[0].longitude);
        }
        "route" => {
            let speed: f64 = args.get(1).ok_or("missing speed")?.parse().map_err(|e| format!("speed: {e}"))?;
            let points = coords(&args[2..])?;
            session.play(&points, speed).await.map_err(|e| format!("route: {e:?}"))?;
            println!("route of {} waypoints at {speed} m/s sent", points.len());
        }
        "clear" => {
            session.clear().await.map_err(|e| format!("clear: {e:?}"))?;
            println!("cleared");
        }
        other => return Err(format!("unknown command {other}")),
    }
    Ok(())
}
