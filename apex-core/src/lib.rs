//! Persistent location simulation over `com.apple.coredevice.locationservice`.
//!
//! Path to the service (no root, no tunneld; the same path works from the
//! phone itself through a loopback VPN and a pairing file):
//!   provider (usbmuxd or TCP + pairing file) -> lockdown CoreDeviceProxy
//!   -> userspace TCP tunnel -> RSD handshake -> CoreDevice location service.
//!
//! The device owns the simulation: it keeps running (including route playback
//! between waypoints at `speed` m/s) after we disconnect, until cleared or the
//! phone reboots. Verified on iOS 27.2.
//!
//! Input schema of `startlocationsimulation`, recovered from the device's Swift
//! Codable errors (idevice and pymobiledevice3 only wrap the scenario listing):
//!   { pacing: {interval|distance: {_0: f64}}, speed: f64,
//!     waypoints: [{latitude: f64, longitude: f64}] }

use idevice::{
    IdeviceError, IdeviceService, ReadWrite, RsdService,
    core_device::CoreDeviceServiceClient,
    core_device_proxy::CoreDeviceProxy,
    cryptexd::{Cryptex1Assets, install_ddi, installed_ddi},
    provider::IdeviceProvider,
    rsd::RsdHandshake,
    tcp::handle::AdapterHandle,
};
use plist::{Dictionary, Value};

pub mod ffi;
pub mod usb;

const SERVICE: &str = "com.apple.coredevice.locationservice";
const FEATURE: &str = "com.apple.coredevice.feature.simulatelocation";
const ACTION_START: &str = "com.apple.coredevice.action.startlocationsimulation";
const ACTION_CLEAR: &str = "com.apple.coredevice.action.clearsimulatedlocation";

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Coordinate {
    pub latitude: f64,
    pub longitude: f64,
}

impl Coordinate {
    pub fn new(latitude: f64, longitude: f64) -> Result<Self, String> {
        if !latitude.is_finite() || !longitude.is_finite() {
            return Err("coordinates must be finite".into());
        }
        if !(-90.0..=90.0).contains(&latitude) {
            return Err(format!("latitude {latitude} outside -90..90"));
        }
        if !(-180.0..=180.0).contains(&longitude) {
            return Err(format!("longitude {longitude} outside -180..180"));
        }
        Ok(Self { latitude, longitude })
    }
}

struct LocationService(CoreDeviceServiceClient<Box<dyn ReadWrite>>);

impl RsdService for LocationService {
    fn rsd_service_name() -> std::borrow::Cow<'static, str> {
        SERVICE.into()
    }

    async fn from_stream(stream: Box<dyn ReadWrite>) -> Result<Self, IdeviceError> {
        Ok(Self(CoreDeviceServiceClient::new(stream).await?))
    }
}

/// Make sure the DeveloperDiskImage cryptex is installed (iOS 27+). The
/// location service only exists while it is; it is lost on reboot.
/// `restore_dir` is an unpacked DDI Restore directory, e.g. Xcode's
/// /Library/Developer/DeveloperDiskImages/iOS_DDI/Restore.
/// Returns true if it had to install it.
pub async fn ensure_ddi(
    provider: &dyn IdeviceProvider,
    restore_dir: &std::path::Path,
) -> Result<bool, IdeviceError> {
    let proxy = CoreDeviceProxy::connect(provider).await?;
    let rsd_port = proxy.tunnel_info().server_rsd_port;
    let mut adapter = proxy.create_software_tunnel()?.to_async_handle();
    let stream = adapter.connect(rsd_port).await?;
    let mut handshake = RsdHandshake::new(stream).await?;
    if installed_ddi(&mut adapter, &mut handshake).await?.is_some() {
        return Ok(false);
    }
    let assets = Cryptex1Assets::load(restore_dir).await?;
    install_ddi(&mut adapter, &mut handshake, &assets).await?;
    Ok(true)
}

/// An open connection to the device's location service. Dropping it is fine:
/// whatever was started keeps running on the device.
pub struct LocationSession {
    // Owns the userspace tunnel; must outlive `service`.
    _adapter: AdapterHandle,
    service: LocationService,
}

impl LocationSession {
    pub async fn connect(provider: &dyn IdeviceProvider) -> Result<Self, IdeviceError> {
        let proxy = CoreDeviceProxy::connect(provider).await?;
        let rsd_port = proxy.tunnel_info().server_rsd_port;
        let mut adapter = proxy.create_software_tunnel()?.to_async_handle();
        let stream = adapter.connect(rsd_port).await?;
        let mut handshake = RsdHandshake::new(stream).await?;
        let service = LocationService::connect_rsd(&mut adapter, &mut handshake).await?;
        Ok(Self { _adapter: adapter, service })
    }

    /// Start a simulation. One waypoint pins the location; several make the
    /// device travel between them at `speed_mps`.
    pub async fn play(&mut self, waypoints: &[Coordinate], speed_mps: f64) -> Result<(), IdeviceError> {
        let mut pacing_value = Dictionary::new();
        pacing_value.insert("_0".into(), Value::Real(1.0));
        let mut pacing = Dictionary::new();
        pacing.insert("interval".into(), Value::Dictionary(pacing_value));

        let points = waypoints
            .iter()
            .map(|c| {
                let mut d = Dictionary::new();
                d.insert("latitude".into(), Value::Real(c.latitude));
                d.insert("longitude".into(), Value::Real(c.longitude));
                Value::Dictionary(d)
            })
            .collect();

        let mut input = Dictionary::new();
        input.insert("pacing".into(), Value::Dictionary(pacing));
        input.insert("speed".into(), Value::Real(speed_mps));
        input.insert("waypoints".into(), Value::Array(points));

        self.service.0.invoke_with_plist_action(FEATURE, input, ACTION_START).await?;
        Ok(())
    }

    pub async fn set(&mut self, at: Coordinate) -> Result<(), IdeviceError> {
        self.play(&[at], 1.0).await
    }

    pub async fn clear(&mut self) -> Result<(), IdeviceError> {
        self.service
            .0
            .invoke_with_plist_action(FEATURE, Dictionary::new(), ACTION_CLEAR)
            .await?;
        Ok(())
    }
}
