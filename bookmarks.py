"""Bookmark storage for Apex Location Director (bookmarks.json next to this file)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

BOOKMARKS_FILE = Path(__file__).resolve().parent / "bookmarks.json"

DEFAULT_BOOKMARKS: dict[str, tuple[float, float]] = {
    "London Eye": (51.5033, -0.1195),
    "Times Square NY": (40.7580, -73.9855),
    "Shibuya Crossing Tokyo": (35.6595, 139.7004),
    "Eiffel Tower Paris": (48.8584, 2.2945),
    "Sydney Opera House": (-33.8568, 151.2153),
    "Golden Gate Bridge SF": (37.8199, -122.4783),
}


class BookmarkStore:
    def __init__(self, path: Path = BOOKMARKS_FILE) -> None:
        self.path = path
        self.items: dict[str, tuple[float, float]] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.items = dict(DEFAULT_BOOKMARKS)
            self.save()
            return
        try:
            raw = json.loads(self.path.read_text())
            # Accepts both {"lat": .., "lng": ..} and the old string-valued format.
            self.items = {
                str(name): (float(v["lat"]), float(v["lng"]))
                for name, v in raw.items()
                if isinstance(v, dict) and str(v.get("lat", "")).strip() and str(v.get("lng", "")).strip()
            }
        except (OSError, ValueError, KeyError, TypeError):
            self.items = dict(DEFAULT_BOOKMARKS)

    def save(self) -> None:
        data = {name: {"lat": lat, "lng": lng} for name, (lat, lng) in self.items.items()}
        # Atomic write so a crash mid-save can't leave a truncated file.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".bookmarks", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=4)
        os.replace(tmp, self.path)

    def put(self, name: str, lat: float, lng: float) -> None:
        self.items[name] = (lat, lng)
        self.save()

    def remove(self, name: str) -> None:
        if self.items.pop(name, None) is not None:
            self.save()

    def names(self) -> list[str]:
        return list(self.items)
