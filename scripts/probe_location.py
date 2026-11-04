"""Probe com.apple.coredevice.locationservice actions: python scripts/probe_location.py ACTION 'JSON_INPUT'"""
import asyncio, json, plistlib, sys
from pymobiledevice3.tunneld.api import get_tunneld_devices
from pymobiledevice3.remote.core_device.location_service import LocationService
F = "com.apple.coredevice.feature.simulatelocation"

def decode_keyed(data):
    p = plistlib.loads(data); objs = p["$objects"]
    def r(x):
        if isinstance(x, plistlib.UID): x = objs[x.data]
        if isinstance(x, dict) and "NS.keys" in x:
            return {r(k): r(v) for k, v in zip(x["NS.keys"], x["NS.objects"])}
        if isinstance(x, dict) and "NS.objects" in x:
            return [r(v) for v in x["NS.objects"]]
        if isinstance(x, dict) and "NS.string" in x:
            return x["NS.string"]
        return x
    return r(p["$top"]["root"])

def find_bytes(o):
    if isinstance(o, bytes): yield o
    elif isinstance(o, dict):
        for v in o.values(): yield from find_bytes(v)
    elif isinstance(o, (list, tuple)):
        for v in o: yield from find_bytes(v)

async def main(action, inp):
    r = (await get_tunneld_devices())[0]
    try:
        async with LocationService(r) as s:
            print("OK ->", await s.invoke(F, inp, action_identifier=action))
    except Exception as e:
        resp = getattr(e, "args", [None])[0]
        import re, ast
        m = re.search(r"request: (\{.*\})$", str(resp), re.S)
        try:
            d = ast.literal_eval(m.group(1))
            for b in find_bytes(d):
                if b.startswith(b"bplist"):
                    print("ERR ->", json.dumps(decode_keyed(b), indent=1, default=str)[:3000])
            print("code:", {k: v for k, v in d["CoreDevice.error"].items() if not isinstance(v, bytes)})
        except Exception:
            print("RAW ERR ->", str(e)[:1500])
    finally:
        await r.close()

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}))
