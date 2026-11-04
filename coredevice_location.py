"""
Client for `com.apple.coredevice.locationservice`, the CoreDevice location
service Xcode's `devicectl` uses on recent iOS (verified on iOS 27.2).

Unlike the DVT LocationSimulation channel, a simulation started here is owned
by the device: it keeps running after the host disconnects (no cable, no
Wi-Fi) until it is cleared or the phone reboots.

pymobiledevice3 only wraps the "available scenarios" action, so the
start/clear actions and the input schema below were worked out by probing the
device's Swift Codable decoding errors:

    startlocationsimulation {
        "pacing":    {"interval": {"_0": <float>}} | {"distance": {"_0": <float>}},
        "speed":     <float m/s>,
        "waypoints": [{"latitude": <float>, "longitude": <float>}, ...],
    }
    clearsimulatedlocation {}
"""

from __future__ import annotations

from typing import Any, Sequence

from pymobiledevice3.remote.core_device.location_service import LocationService
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService

FEATURE = "com.apple.coredevice.feature.simulatelocation"
ACTION_START = "com.apple.coredevice.action.startlocationsimulation"
ACTION_CLEAR = "com.apple.coredevice.action.clearsimulatedlocation"
ACTION_SCENARIOS = "com.apple.coredevice.action.availablelocationscenarios"


class CoreDeviceLocation:
    def __init__(self, rsd: RemoteServiceDiscoveryService) -> None:
        self.rsd = rsd

    @staticmethod
    def supported(rsd: RemoteServiceDiscoveryService) -> bool:
        service = rsd.peer_info.get("Services", {}).get(LocationService.SERVICE_NAME, {})
        return FEATURE in service.get("Properties", {}).get("Features", [])

    async def _invoke(self, action: str, input_: dict[str, Any]) -> Any:
        # One short-lived connection per call: the simulation lives on the
        # device, so there is nothing to keep open between calls.
        async with LocationService(self.rsd) as service:
            return await service.invoke(FEATURE, input_, action_identifier=action)

    async def play(self, waypoints: Sequence[tuple[float, float]], speed_mps: float,
                   pacing: str = "interval", pacing_value: float = 1.0) -> None:
        await self._invoke(ACTION_START, {
            "pacing": {pacing: {"_0": float(pacing_value)}},
            "speed": float(speed_mps),
            "waypoints": [{"latitude": float(lat), "longitude": float(lng)} for lat, lng in waypoints],
        })

    async def set(self, lat: float, lng: float) -> None:
        await self.play([(lat, lng)], speed_mps=1.0)

    async def clear(self) -> None:
        await self._invoke(ACTION_CLEAR, {})

    async def scenarios(self) -> list[str]:
        out = await self._invoke(ACTION_SCENARIOS, {})
        return [s.get("name", "") for s in out.get("scenarios", [])]
