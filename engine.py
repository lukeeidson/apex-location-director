"""
Apex Location Director - device engine.

Everything here is async and is meant to run on one dedicated asyncio loop
(the GUI owns that loop on a background thread). Written against
pymobiledevice3 11.x.

Transport by iOS version:
  * iOS < 17   lockdown over usbmuxd, classic DeveloperDiskImage (+ .signature),
               `com.apple.dt.simulatelocation`. The simulated location PERSISTS
               after the cable is pulled (until reboot or an explicit clear).
  * iOS 17+    Personalized DDI (image + BuildManifest + trustcache, ticket
               signed per-device by Apple's TSS server; a Cryptex from iOS 27),
               RSD tunnel from `tunneld`. Location goes through the CoreDevice
               location service when the device offers it: that simulation
               PERSISTS with no connection at all (verified on iOS 27.2).
               Otherwise falls back to DVT `LocationSimulation`, which iOS
               resets as soon as the connection closes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import plistlib
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from packaging.version import Version
from pymobiledevice3.exceptions import (
    AlreadyMountedError,
    NoDeviceConnectedError,
    NoSuchBuildIdentityError,
    PasswordRequiredError,
    PyMobileDevice3Exception,
    TunneldConnectionError,
)
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.services.mobile_image_mounter import (
    DeveloperDiskImageMounter,
    PersonalizedImageMounter,
    auto_mount,
    auto_mount_developer,
    fetch_personalized_ddi,
)
from pymobiledevice3.tunneld.api import get_tunneld_device_by_udid

from coredevice_location import CoreDeviceLocation

log = logging.getLogger("apex.engine")

PERSONALIZED_MIN = Version("17.0")
CRYPTEX_MIN = Version("27.0")
EARTH_RADIUS_M = 6_371_000.0

XCODE_SIGNATURE_HINT = (
    "Please launch Xcode once with your device connected to auto-generate "
    "the required personalization signature."
)
TUNNELD_HINT = (
    "iOS 17+ needs the tunnel daemon. In Terminal run:  "
    "sudo python3 -m pymobiledevice3 remote tunneld   (leave it running), then Connect again."
)

# Where Xcode keeps developer disk images on macOS.
PERSONALIZED_DDI_ROOTS = [
    Path.home() / "Library/Developer/DeveloperDiskImages",
    Path("/Library/Developer/DeveloperDiskImages"),  # where Xcode 15+ actually installs iOS_DDI
    Path.home() / ".pymobiledevice3/Xcode_iOS_DDI_Personalized",
]
NON_IOS_DDI_TAGS = ("watchOS", "tvOS", "xrOS", "visionOS", "macOS")
XCODE_APPS_GLOB = "Applications/Xcode*.app"  # relative to /
DEVICE_SUPPORT_SUBPATH = "Contents/Developer/Platforms/iPhoneOS.platform/DeviceSupport"


class EngineError(Exception):
    """A failure with a message that is safe to show to the user as-is."""


@dataclass
class DeviceInfo:
    udid: str
    name: str
    product_type: str
    version: str
    build: str

    @property
    def label(self) -> str:
        return f"{self.name} ({self.product_type}, iOS {self.version} {self.build})"


# --------------------------------------------------------------------------
# Geography
# --------------------------------------------------------------------------

def validate_coordinate(lat: float, lng: float) -> None:
    if not (math.isfinite(lat) and math.isfinite(lng)):
        raise EngineError("Coordinates must be finite numbers.")
    if not -90.0 <= lat <= 90.0:
        raise EngineError(f"Latitude {lat} is outside -90..90.")
    if not -180.0 <= lng <= 180.0:
        raise EngineError(f"Longitude {lng} is outside -180..180.")


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def great_circle_point(lat1: float, lng1: float, lat2: float, lng2: float, f: float) -> tuple[float, float]:
    """Point at fraction f along the great circle. Unlike linear lat/lng
    interpolation this follows the real shortest path and crosses the
    antimeridian correctly."""
    p1, l1, p2, l2 = map(math.radians, (lat1, lng1, lat2, lng2))
    d = 2 * math.asin(min(1.0, math.sqrt(
        math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2)))
    if d < 1e-12:
        return lat1, lng1
    a = math.sin((1 - f) * d) / math.sin(d)
    b = math.sin(f * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    lat = math.degrees(math.atan2(z, math.hypot(x, y)))
    lng = (math.degrees(math.atan2(y, x)) + 540.0) % 360.0 - 180.0
    return lat, lng


# --------------------------------------------------------------------------
# Xcode DDI cache discovery
# --------------------------------------------------------------------------

@dataclass
class PersonalizedDdiFiles:
    image: Path
    build_manifest: Path
    trust_cache: Path


def _manifest_component_path(manifest: dict, component: str) -> Optional[str]:
    for identity in manifest.get("BuildIdentities", []):
        path = identity.get("Manifest", {}).get(component, {}).get("Info", {}).get("Path")
        if path:
            return path
    return None


def _resolve_personalized(manifest_path: Path) -> Optional[PersonalizedDdiFiles]:
    try:
        manifest = plistlib.loads(manifest_path.read_bytes())
    except Exception:
        return None
    root = manifest_path.parent
    image_rel = _manifest_component_path(manifest, "PersonalizedDMG")
    tc_rel = _manifest_component_path(manifest, "LoadableTrustCache")
    image = root / image_rel if image_rel else None
    trust_cache = root / tc_rel if tc_rel else None
    # Layout used by pymobiledevice3's own cache, and a safety net for odd manifests.
    if image is None or not image.is_file():
        dmgs = sorted(p for p in root.glob("*.dmg") if p.is_file())
        image = dmgs[0] if dmgs else None
    if trust_cache is None or not trust_cache.is_file():
        tcs = sorted(root.glob("*.trustcache"))
        trust_cache = tcs[0] if tcs else None
    if image and trust_cache and image.is_file() and trust_cache.is_file():
        return PersonalizedDdiFiles(image, manifest_path, trust_cache)
    return None


def find_personalized_ddis() -> Iterator[PersonalizedDdiFiles]:
    """Yield every complete Personalized DDI found in the Xcode caches. The
    per-device signature is NOT on disk: it is an IM4M ticket Apple's TSS
    server issues for this device's ECID + nonce at mount time, so what we
    look for is the image, its BuildManifest and its trustcache."""
    seen: set[Path] = set()
    for root in PERSONALIZED_DDI_ROOTS:
        if not root.is_dir():
            continue
        try:
            manifests = list(root.rglob("BuildManifest.plist"))
        except OSError:
            continue
        for manifest_path in manifests:
            if any(tag in str(manifest_path) for tag in NON_IOS_DDI_TAGS):
                continue
            resolved = manifest_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files = _resolve_personalized(manifest_path)
            if files:
                yield files


def find_legacy_ddi(version: Version, build: str) -> Optional[tuple[Path, Path]]:
    """Find DeveloperDiskImage.dmg + .signature for iOS < 17 in any installed
    Xcode. Prefers a folder that names the exact build, e.g. '16.4 (20E247)'."""
    major_minor = f"{version.major}.{version.minor}"
    candidates: list[Path] = []
    for xcode in sorted(Path("/").glob(XCODE_APPS_GLOB)):
        support = xcode / DEVICE_SUPPORT_SUBPATH
        if not support.is_dir():
            continue
        for folder in support.iterdir():
            if folder.name == major_minor or folder.name.startswith(major_minor + " "):
                candidates.append(folder)
    candidates.sort(key=lambda p: build not in p.name)
    for folder in candidates:
        image = folder / "DeveloperDiskImage.dmg"
        signature = folder / "DeveloperDiskImage.dmg.signature"
        if image.is_file() and signature.is_file():
            return image, signature
    return None


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

StatusCallback = Callable[[str], None]


class LocationEngine:
    def __init__(self) -> None:
        self.lockdown = None
        self.rsd = None
        self.info: Optional[DeviceInfo] = None
        self._stack: Optional[contextlib.AsyncExitStack] = None
        self._dvt_sim: Optional[LocationSimulation] = None
        self._cd: Optional[CoreDeviceLocation] = None
        self._route_task: Optional[asyncio.Task] = None
        self.last_position: Optional[tuple[float, float]] = None

    @property
    def version(self) -> Version:
        return Version(self.info.version) if self.info else Version("0")

    @property
    def is_ready(self) -> bool:
        return self.info is not None and (
            self.version < PERSONALIZED_MIN or self._dvt_sim is not None or self._cd is not None)

    @property
    def persists_after_unplug(self) -> bool:
        return self.version < PERSONALIZED_MIN or self._cd is not None

    # ---- connection -----------------------------------------------------

    async def connect(self, status: StatusCallback) -> DeviceInfo:
        await self.shutdown()
        status("Scanning USB bus (usbmuxd)...")
        try:
            self.lockdown = await create_using_usbmux(autopair=True)
        except NoDeviceConnectedError:
            raise EngineError("No device found. Plug the iPhone in via USB and unlock it.")
        except PasswordRequiredError:
            raise EngineError("Unlock the iPhone and tap 'Trust This Computer', then try again.")
        except PyMobileDevice3Exception as e:
            raise EngineError(f"Pairing/lockdown failed: {e!r}")

        ld = self.lockdown
        self.info = DeviceInfo(
            udid=ld.udid,
            name=ld.all_values.get("DeviceName", "iPhone"),
            product_type=ld.product_type or "?",
            version=ld.product_version,
            build=ld.product_build_version or "?",
        )
        status(f"Found {self.info.label}")

        if self.version >= Version("16.0") and not await ld.get_developer_mode_status():
            raise EngineError(
                "Developer Mode is off. Enable it in Settings > Privacy & Security > "
                "Developer Mode (the phone will reboot), then reconnect.")

        if self.version >= PERSONALIZED_MIN:
            await self._open_tunnel(status)

        await self._mount_ddi(status)

        if self.version >= PERSONALIZED_MIN and CoreDeviceLocation.supported(self.rsd):
            status("Using CoreDevice location service (persists after disconnect).")
            self._cd = CoreDeviceLocation(self.rsd)
        elif self.version >= PERSONALIZED_MIN:
            status("Opening DVT LocationSimulation channel...")
            self._stack = contextlib.AsyncExitStack()
            dvt = await self._stack.enter_async_context(DvtProvider(self.rsd))
            self._dvt_sim = await self._stack.enter_async_context(LocationSimulation(dvt))
        return self.info

    async def _open_tunnel(self, status: StatusCallback) -> None:
        status("Looking up RSD tunnel from tunneld...")
        try:
            self.rsd = await get_tunneld_device_by_udid(self.info.udid)
        except TunneldConnectionError:
            raise EngineError(TUNNELD_HINT)
        if self.rsd is None:
            raise EngineError(
                "tunneld is running but has no tunnel for this device yet. Keep the phone "
                "unlocked and plugged in, wait ~10s, then Connect again.")

    async def _mount_ddi(self, status: StatusCallback) -> None:
        status("Mounting developer disk image...")
        try:
            if self.version >= CRYPTEX_MIN:
                await auto_mount(self.rsd)
            elif self.version >= PERSONALIZED_MIN:
                await self._mount_personalized(status)
            else:
                await self._mount_legacy(status)
        except AlreadyMountedError:
            status("Developer disk image already mounted.")

    async def _mount_personalized(self, status: StatusCallback) -> None:
        async with PersonalizedImageMounter(lockdown=self.lockdown) as mounter:
            if await mounter.is_image_mounted(PersonalizedImageMounter.IMAGE_TYPE):
                raise AlreadyMountedError()

        errors: list[str] = []
        for files in find_personalized_ddis():
            status(f"Personalizing DDI from {files.build_manifest.parent} (Apple TSS)...")
            try:
                async with PersonalizedImageMounter(lockdown=self.lockdown) as mounter:
                    await mounter.mount(files.image, files.build_manifest, files.trust_cache)
                return
            except AlreadyMountedError:
                raise
            except NoSuchBuildIdentityError:
                errors.append(f"{files.build_manifest}: does not support this board")
            except Exception as e:  # noqa: BLE001 - try the next cache entry
                errors.append(f"{files.build_manifest}: {e!r}")

        status("No usable Xcode DDI cache; downloading personalized DDI...")
        try:
            image, manifest, trust_cache = await asyncio.to_thread(fetch_personalized_ddi)
            async with PersonalizedImageMounter(lockdown=self.lockdown) as mounter:
                await mounter.mount(image, manifest, trust_cache)
        except AlreadyMountedError:
            raise
        except Exception as e:
            log.warning("DDI attempts failed: %s; download: %r", errors, e)
            raise EngineError(XCODE_SIGNATURE_HINT)

    async def _mount_legacy(self, status: StatusCallback) -> None:
        found = find_legacy_ddi(self.version, self.info.build)
        if found:
            image, signature = found
            status(f"Mounting {image.parent.name} DeveloperDiskImage from Xcode...")
            async with DeveloperDiskImageMounter(lockdown=self.lockdown) as mounter:
                await mounter.mount(image, signature)
            return
        status("No matching Xcode DeviceSupport; downloading DeveloperDiskImage...")
        try:
            await auto_mount_developer(self.lockdown)
        except AlreadyMountedError:
            raise
        except Exception as e:
            log.warning("Legacy DDI download failed: %r", e)
            raise EngineError(XCODE_SIGNATURE_HINT)

    # ---- location -------------------------------------------------------

    async def set_location(self, lat: float, lng: float) -> None:
        validate_coordinate(lat, lng)
        if not self.is_ready:
            raise EngineError("Not connected.")
        if self._cd is not None:
            await self._cd.set(lat, lng)
        elif self._dvt_sim is not None:
            await self._dvt_sim.set(lat, lng)
        else:
            await self._legacy_send(struct.pack(">I", 0) + _lp(str(lat)) + _lp(str(lng)))
        self.last_position = (lat, lng)

    async def clear_location(self) -> None:
        await self.stop_route()
        if self._cd is not None:
            await self._cd.clear()
        elif self._dvt_sim is not None:
            await self._dvt_sim.clear()
        elif self.lockdown is not None:
            await self._legacy_send(struct.pack(">I", 1))
        self.last_position = None

    async def _legacy_send(self, payload: bytes) -> None:
        # pymobiledevice3's DtSimulateLocation never closes its connection; at
        # one update per second during a route that leaks sockets, so do it here.
        service = await self.lockdown.start_lockdown_developer_service("com.apple.dt.simulatelocation")
        try:
            await service.sendall(payload)
        finally:
            await service.close()

    # ---- routes ---------------------------------------------------------

    async def start_route(self, start: tuple[float, float], end: tuple[float, float],
                          speed_mps: float, status: StatusCallback, tick_s: float = 1.0) -> None:
        validate_coordinate(*start)
        validate_coordinate(*end)
        if speed_mps <= 0:
            raise EngineError("Speed must be positive.")
        if not self.is_ready:
            raise EngineError("Not connected.")
        await self.stop_route()
        self._route_task = asyncio.create_task(self._run_route(start, end, speed_mps, status, tick_s))

    async def _run_route(self, start, end, speed_mps, status, tick_s) -> None:
        total = haversine_m(*start, *end)
        steps = max(1, math.ceil(total / (speed_mps * tick_s)))
        eta_s = total / speed_mps
        status(f"Route: {total / 1000:.2f} km at {speed_mps} m/s, ETA {eta_s / 60:.1f} min")
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        try:
            for i in range(steps + 1):
                lat, lng = great_circle_point(*start, *end, i / steps)
                await self.set_location(lat, lng)
                remaining = total * (1 - i / steps)
                status(f"Moving {lat:.5f}, {lng:.5f}  ({i}/{steps}, {remaining:.0f} m left)")
                # Schedule against a fixed clock so slow sends don't stretch the trip.
                await asyncio.sleep(max(0.0, t0 + (i + 1) * tick_s - loop.time()))
            status(f"Arrived at {end[0]:.5f}, {end[1]:.5f}")
        except asyncio.CancelledError:
            status("Route stopped.")
            raise
        except Exception as e:  # surface instead of dying silently in the task
            status(f"Route aborted: {e}")

    async def stop_route(self) -> None:
        task, self._route_task = self._route_task, None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @property
    def route_running(self) -> bool:
        return self._route_task is not None and not self._route_task.done()

    # ---- teardown -------------------------------------------------------

    async def shutdown(self) -> None:
        """Close all connections. The simulated location stays in effect
        afterwards unless the DVT fallback was in use (see persists_after_unplug)."""
        await self.stop_route()
        if self._stack is not None:
            with contextlib.suppress(Exception):
                await self._stack.aclose()
        for conn in (self.rsd, self.lockdown):
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.close()
        self._stack = self._dvt_sim = self._cd = self.rsd = self.lockdown = None
        self.info = None


def _lp(text: str) -> bytes:
    data = text.encode()
    return struct.pack(">I", len(data)) + data
