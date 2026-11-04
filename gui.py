"""
Apex Location Director - GUI.

Threading model: Tk runs on the main thread; all device I/O runs on one
asyncio loop in a background thread. The two never touch each other's
objects directly: the GUI submits coroutines with run_coroutine_threadsafe,
and the loop reports back through a queue the Tk thread drains every 50 ms
(Tk widgets must only be touched from the main thread).
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import subprocess
import threading
from tkinter import messagebox
from typing import Callable, Optional

import customtkinter as ctk

from bookmarks import BookmarkStore
from engine import EngineError, LocationEngine, validate_coordinate

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

OK, ERR, INFO = "#2ECC71", "#E74C3C", None
SPEEDS = {"Walk (1.4 m/s)": 1.4, "Cycle (5.5 m/s)": 5.5, "Drive (15.0 m/s)": 15.0, "Custom": None}
BOOKMARK_PLACEHOLDER = "Select a place..."


class ApexApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.engine = LocationEngine()
        self.bookmarks = BookmarkStore()
        self.ui_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self.caffeinate: Optional[subprocess.Popen] = None

        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run_loop, daemon=True, name="device-io").start()

        self.title("Apex Location Director")
        self.geometry("560x720")
        self.minsize(520, 680)
        self._build()
        self._set_connected(False)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(50, self._drain_ui_queue)

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        pad = {"padx": 10, "pady": 5}
        self.status = ctk.CTkLabel(self, text="Plug in your iPhone, unlock it, then Connect.",
                                   wraplength=520, justify="left", font=("", 13, "italic"))
        self.status.pack(fill="x", padx=20, pady=(15, 4))
        self.device_label = ctk.CTkLabel(self, text="No device", font=("", 12, "bold"))
        self.device_label.pack(padx=20, pady=(0, 8))

        conn = ctk.CTkFrame(self)
        conn.pack(fill="x", padx=20, pady=5)
        conn.grid_columnconfigure((0, 1), weight=1)
        self.connect_btn = ctk.CTkButton(conn, text="Connect", command=self.on_connect)
        self.connect_btn.grid(row=0, column=0, sticky="ew", **pad)
        self.disconnect_btn = ctk.CTkButton(conn, text="Disconnect", command=self.on_disconnect,
                                            fg_color="gray40", hover_color="gray30")
        self.disconnect_btn.grid(row=0, column=1, sticky="ew", **pad)

        # Target
        tgt = ctk.CTkFrame(self)
        tgt.pack(fill="x", padx=20, pady=5)
        tgt.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(tgt, text="Target", font=("", 13, "bold")).grid(row=0, column=0, sticky="w", **pad)
        ctk.CTkLabel(tgt, text="Latitude").grid(row=1, column=0, sticky="w", **pad)
        self.lat_entry = ctk.CTkEntry(tgt, placeholder_text="-90 .. 90  e.g. 37.7749")
        self.lat_entry.grid(row=1, column=1, sticky="ew", **pad)
        ctk.CTkLabel(tgt, text="Longitude").grid(row=2, column=0, sticky="w", **pad)
        self.lng_entry = ctk.CTkEntry(tgt, placeholder_text="-180 .. 180  e.g. -122.4194")
        self.lng_entry.grid(row=2, column=1, sticky="ew", **pad)
        self.teleport_btn = ctk.CTkButton(tgt, text="Teleport", command=self.on_teleport)
        self.teleport_btn.grid(row=3, column=0, columnspan=2, sticky="ew", **pad)

        # Bookmarks
        bm = ctk.CTkFrame(self)
        bm.pack(fill="x", padx=20, pady=5)
        bm.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(bm, text="Saved places").grid(row=0, column=0, sticky="w", **pad)
        self.bookmark_menu = ctk.CTkOptionMenu(bm, values=self.bookmarks.names() or [BOOKMARK_PLACEHOLDER],
                                               command=self.on_bookmark_select)
        self.bookmark_menu.set(BOOKMARK_PLACEHOLDER)
        self.bookmark_menu.grid(row=0, column=1, sticky="ew", **pad)
        ctk.CTkButton(bm, text="Delete", width=70, fg_color="gray40", hover_color="gray30",
                      command=self.on_bookmark_delete).grid(row=0, column=2, **pad)
        self.bookmark_name = ctk.CTkEntry(bm, placeholder_text="Name for current target...")
        self.bookmark_name.grid(row=1, column=0, columnspan=2, sticky="ew", **pad)
        ctk.CTkButton(bm, text="Save Current", width=70, command=self.on_bookmark_save).grid(row=1, column=2, **pad)

        # Route
        rt = ctk.CTkFrame(self)
        rt.pack(fill="x", padx=20, pady=5)
        rt.grid_columnconfigure((1, 3), weight=1)
        ctk.CTkLabel(rt, text="Route (start -> target)", font=("", 13, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w", **pad)
        ctk.CTkLabel(rt, text="Start lat").grid(row=1, column=0, sticky="w", **pad)
        self.start_lat_entry = ctk.CTkEntry(rt, width=110)
        self.start_lat_entry.grid(row=1, column=1, sticky="ew", **pad)
        ctk.CTkLabel(rt, text="Start lng").grid(row=1, column=2, sticky="w", **pad)
        self.start_lng_entry = ctk.CTkEntry(rt, width=110)
        self.start_lng_entry.grid(row=1, column=3, sticky="ew", **pad)
        ctk.CTkButton(rt, text="Use current position as start", fg_color="gray40", hover_color="gray30",
                      command=self.on_use_current).grid(row=2, column=0, columnspan=4, sticky="ew", **pad)
        ctk.CTkLabel(rt, text="Speed").grid(row=3, column=0, sticky="w", **pad)
        self.speed_menu = ctk.CTkOptionMenu(rt, values=list(SPEEDS), command=self.on_speed_select)
        self.speed_menu.grid(row=3, column=1, sticky="ew", **pad)
        self.custom_speed = ctk.CTkEntry(rt, placeholder_text="m/s", width=80, state="disabled")
        self.custom_speed.grid(row=3, column=2, columnspan=2, sticky="ew", **pad)
        self.route_btn = ctk.CTkButton(rt, text="Start Route", command=self.on_route)
        self.route_btn.grid(row=4, column=0, columnspan=2, sticky="ew", **pad)
        self.stop_btn = ctk.CTkButton(rt, text="Stop Route", command=self.on_stop_route,
                                      fg_color="gray40", hover_color="gray30")
        self.stop_btn.grid(row=4, column=2, columnspan=2, sticky="ew", **pad)

        # Session
        ss = ctk.CTkFrame(self)
        ss.pack(fill="x", padx=20, pady=5)
        ss.grid_columnconfigure((0, 1), weight=1)
        self.keep_awake = ctk.CTkCheckBox(ss, text="Keep Mac awake while holding a location",
                                          command=self.on_keep_awake)
        self.keep_awake.grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        self.reset_btn = ctk.CTkButton(ss, text="Reset to real GPS", command=self.on_reset,
                                       fg_color="#C0392B", hover_color="#94281B")
        self.reset_btn.grid(row=1, column=0, columnspan=2, sticky="ew", **pad)

        self.device_buttons = [self.teleport_btn, self.route_btn, self.stop_btn, self.reset_btn, self.disconnect_btn]

    # --------------------------------------------------------- thread plumbing

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                self.ui_queue.get_nowait()()
        except queue.Empty:
            pass
        self.after(50, self._drain_ui_queue)

    def ui(self, fn: Callable[[], None]) -> None:
        """Run fn on the Tk thread. Safe to call from any thread."""
        self.ui_queue.put(fn)

    def set_status(self, text: str, color: Optional[str] = INFO) -> None:
        def apply() -> None:
            self.status.configure(text=text, text_color=color or ("gray10", "gray90"))
        self.ui(apply)

    def submit(self, coro, on_done: Optional[Callable[[object], None]] = None,
               on_error: Optional[Callable[[], None]] = None) -> None:
        """Run a coroutine on the device loop; report EngineError/other errors in the status bar."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def finished(f) -> None:
            try:
                result = f.result()
            except EngineError as e:
                self.set_status(str(e), ERR)
                if on_error:
                    self.ui(on_error)
            except BaseException as e:  # noqa: BLE001 - last-resort reporting, incl. cancellation
                logging.exception("device task failed")
                self.set_status(f"Unexpected error: {e!r}", ERR)
                if on_error:
                    self.ui(on_error)
            else:
                if on_done:
                    self.ui(lambda: on_done(result))
        future.add_done_callback(finished)

    # ------------------------------------------------------------ helpers

    def _set_connected(self, connected: bool) -> None:
        for b in self.device_buttons:
            b.configure(state="normal" if connected else "disabled")
        self.connect_btn.configure(text="Reconnect" if connected else "Connect")

    def _read_coord(self, lat_entry: ctk.CTkEntry, lng_entry: ctk.CTkEntry, what: str) -> Optional[tuple[float, float]]:
        try:
            lat, lng = float(lat_entry.get().strip()), float(lng_entry.get().strip())
            validate_coordinate(lat, lng)
            return lat, lng
        except ValueError:
            self.set_status(f"{what}: enter decimal numbers, e.g. 51.5033 and -0.1195.", ERR)
        except EngineError as e:
            self.set_status(f"{what}: {e}", ERR)
        return None

    @staticmethod
    def _fill(entry: ctk.CTkEntry, value: float) -> None:
        entry.delete(0, "end")
        entry.insert(0, f"{value:.6f}")

    def _status_cb(self, text: str) -> None:
        self.set_status(text)

    # ------------------------------------------------------------ handlers

    def on_connect(self) -> None:
        self._set_connected(False)
        self.connect_btn.configure(state="disabled")

        def done(info) -> None:
            self._set_connected(True)
            self.connect_btn.configure(state="normal")
            self.device_label.configure(text=info.label)
            if self.engine.persists_after_unplug:
                note = "Location will stay set after you unplug (until reboot or Reset)."
            else:
                note = ("iOS 17+: the location holds only while this app stays connected "
                        "(USB, or Wi-Fi via tunneld). Quitting or sleeping the Mac reverts it.")
            self.set_status(f"Ready. {note}", OK)

        def failed() -> None:
            self.connect_btn.configure(state="normal")
            self.device_label.configure(text="No device")

        self.submit(self.engine.connect(self._status_cb), done, failed)

    def on_disconnect(self) -> None:
        self._set_connected(False)
        self.device_label.configure(text="No device")
        self.submit(self.engine.shutdown(), lambda _: self.set_status("Disconnected."))

    def on_teleport(self) -> None:
        coord = self._read_coord(self.lat_entry, self.lng_entry, "Target")
        if coord:
            async def go():
                await self.engine.stop_route()
                await self.engine.set_location(*coord)
            self.submit(go(), lambda _: self.set_status(f"Location set to {coord[0]:.6f}, {coord[1]:.6f}", OK))

    def on_route(self) -> None:
        start = self._read_coord(self.start_lat_entry, self.start_lng_entry, "Route start")
        end = start and self._read_coord(self.lat_entry, self.lng_entry, "Target")
        if not (start and end):
            return
        speed = SPEEDS[self.speed_menu.get()]
        if speed is None:
            try:
                speed = float(self.custom_speed.get())
                if not 0 < speed <= 350:
                    raise ValueError
            except ValueError:
                self.set_status("Custom speed must be a number of m/s between 0 and 350.", ERR)
                return
        self.submit(self.engine.start_route(start, end, speed, self._status_cb))

    def on_stop_route(self) -> None:
        self.submit(self.engine.stop_route())

    def on_reset(self) -> None:
        self.submit(self.engine.clear_location(), lambda _: self.set_status("Simulation cleared; device uses real GPS.", OK))

    def on_use_current(self) -> None:
        pos = self.engine.last_position
        if pos is None:
            self.set_status("No simulated position yet - teleport somewhere first.", ERR)
            return
        self._fill(self.start_lat_entry, pos[0])
        self._fill(self.start_lng_entry, pos[1])

    def on_speed_select(self, choice: str) -> None:
        self.custom_speed.configure(state="normal" if SPEEDS[choice] is None else "disabled")

    def on_keep_awake(self) -> None:
        if self.keep_awake.get():
            # -i: no idle sleep, -w: exit automatically when this app exits.
            # Closing the lid on battery still sleeps the Mac (macOS enforces that).
            self.caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        elif self.caffeinate:
            self.caffeinate.terminate()
            self.caffeinate = None

    # ------------------------------------------------------------ bookmarks

    def _refresh_bookmarks(self, selected: str = BOOKMARK_PLACEHOLDER) -> None:
        self.bookmark_menu.configure(values=self.bookmarks.names() or [BOOKMARK_PLACEHOLDER])
        self.bookmark_menu.set(selected)

    def on_bookmark_select(self, name: str) -> None:
        coord = self.bookmarks.items.get(name)
        if coord:
            self._fill(self.lat_entry, coord[0])
            self._fill(self.lng_entry, coord[1])

    def on_bookmark_save(self) -> None:
        name = self.bookmark_name.get().strip()
        if not name or name == BOOKMARK_PLACEHOLDER:
            self.set_status("Type a name for the bookmark first.", ERR)
            return
        coord = self._read_coord(self.lat_entry, self.lng_entry, "Bookmark")
        if not coord:
            return
        try:
            self.bookmarks.put(name, *coord)
        except OSError as e:
            self.set_status(f"Could not write bookmarks.json: {e}", ERR)
            return
        self.bookmark_name.delete(0, "end")
        self._refresh_bookmarks(name)
        self.set_status(f"Saved '{name}'.", OK)

    def on_bookmark_delete(self) -> None:
        name = self.bookmark_menu.get()
        if name in self.bookmarks.items and messagebox.askyesno("Delete bookmark", f"Delete '{name}'?"):
            self.bookmarks.remove(name)
            self._refresh_bookmarks()

    # ------------------------------------------------------------ shutdown

    def on_close(self) -> None:
        if self.engine.is_ready and not self.engine.persists_after_unplug and self.engine.last_position:
            if not messagebox.askyesno(
                    "Quit?", "On iOS 17+ the phone returns to real GPS when this app quits. Quit anyway?"):
                return
        future = asyncio.run_coroutine_threadsafe(self.engine.shutdown(), self.loop)
        try:
            future.result(timeout=5)
        except Exception:  # noqa: BLE001 - quitting regardless
            pass
        if self.caffeinate:
            self.caffeinate.terminate()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.destroy()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ApexApp().mainloop()
