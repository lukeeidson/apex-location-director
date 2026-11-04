"""Try enum case names for a CoreDevice location input field.
usage: python scripts/brute_keys.py 'JSON_TEMPLATE_WITH_@KEY@' name1 name2 ..."""
import asyncio, ast, json, re, sys
from pymobiledevice3.tunneld.api import get_tunneld_devices
from pymobiledevice3.remote.core_device.location_service import LocationService
F = "com.apple.coredevice.feature.simulatelocation"
A = "com.apple.coredevice.action.startlocationsimulation"

async def main(template, names):
    r = (await get_tunneld_devices())[0]
    async with LocationService(r) as s:
        for n in names:
            inp = json.loads(template.replace("@KEY@", n))
            try:
                print(n, "OK ->", await s.invoke(F, inp, action_identifier=A))
                continue
            except Exception as e:
                text = str(e.args[0]) if e.args else repr(e)
            m = re.search(r"request: (\{.*\})$", text, re.S)
            try:
                ui = ast.literal_eval(m.group(1))["CoreDevice.error"].get("userInfo", {})
                desc, path = ui.get("NSDebugDescription", "?"), ui.get("NSCodingPath", "")
            except Exception:
                desc, path = text[:200], ""
            if "Invalid number of keys" not in desc:
                print(f"{n}: {desc} | {path}"[:400])
    await r.close()

asyncio.run(main(sys.argv[1], sys.argv[2:]))
