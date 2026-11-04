"""Manual end-to-end check against a connected device: python scripts/device_test.py [step]"""
import asyncio, logging, sys
sys.path.insert(0, __import__("os").path.dirname(__file__) + "/..")
from engine import LocationEngine

async def main(step):
    e = LocationEngine()
    try:
        info = await e.connect(lambda m: print("  status:", m))
        print("CONNECTED:", info.label, "| ready:", e.is_ready)
        if step in ("teleport", "route"):
            await e.set_location(51.5033, -0.1195)
            print("TELEPORTED to London Eye; holding 20s - check Maps on the phone")
            await asyncio.sleep(20)
        if step == "route":
            await e.start_route((51.5033, -0.1195), (51.5010, -0.1246), 15.0, lambda m: print("  route:", m))
            await asyncio.sleep(30)
        if step in ("teleport", "route"):
            await e.clear_location(); print("CLEARED")
        if step == "hold":
            await e.set_location(35.6595, 139.7004)
            print("SET Shibuya Tokyo; disconnecting WITHOUT clear")
        if step == "hardkill":
            await e.set_location(48.8584, 2.2945)
            print("SET Eiffel Tower; hard-killing process (os._exit) WITHOUT clear")
            sys.stdout.flush()
            __import__("os")._exit(0)
        if step == "wifihold":
            await e.set_location(35.6595, 139.7004)
            print("SET Shibuya Tokyo over current transport; holding 60s")
            for i in range(6):
                await asyncio.sleep(10)
                await e.set_location(35.6595, 139.7004)  # also proves the link is still alive
                print(f"  still holding ({(i + 1) * 10}s)")
            await e.clear_location(); print("CLEARED")
        if step == "deviceroute":
            # Only two waypoints: tests whether the phone interpolates between them on its own.
            await e._cd.play([(40.7580, -73.9855), (40.7681, -73.9819)], speed_mps=15.0)
            print("SENT Times Square -> Central Park @15 m/s; disconnecting now")
        if step == "clear":
            await e.clear_location(); print("CLEARED")
    finally:
        await e.shutdown()

logging.basicConfig(level=logging.WARNING)
asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "connect"))
