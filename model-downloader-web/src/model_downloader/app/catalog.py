"""PVC-backed model catalog with GPU-tier grouping.

The catalog is a JSON file on a small dedicated PVC.  If the file is
missing it is seeded from seed_catalog.json bundled in the image.  On
every start, seed entries not yet present (and not explicitly removed)
are merged in, so new seed entries ship with an image upgrade without
overwriting user edits or resurrecting removed entries.  Each entry is a
full packaged_models config plus a catalog_id (uuid or seed-* name) and
a tier (h200 | rtx-pro-6000 | l40s).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

SEED_FILE = Path(__file__).parent / "seed_catalog.json"
TIERS = ["h200", "rtx-pro-6000", "l40s"]
TIER_LABELS = {"h200": "H200", "rtx-pro-6000": "RTX Pro 6000", "l40s": "L40S"}


class Catalog:
    def __init__(self, path: str):
        self.path = Path(path)
        self.removed_path = self.path.with_name("removed_seeds.json")
        self.entries: list[dict] = []
        self._removed_seed_ids: set[str] = set()
        self._seed_ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        self._removed_seed_ids = self._read_removed_seeds()
        if self.path.exists():
            self.entries = json.loads(self.path.read_text())
        else:
            self.entries = []
        changed = self._merge_seed()
        if changed or not self.path.exists():
            self._save()

    def _read_removed_seeds(self) -> set[str]:
        if self.removed_path.exists():
            return set(json.loads(self.removed_path.read_text()))
        return set()

    def _write_removed_seeds(self) -> None:
        self.removed_path.parent.mkdir(parents=True, exist_ok=True)
        self.removed_path.write_text(json.dumps(sorted(self._removed_seed_ids), indent=2))

    def _merge_seed(self) -> bool:
        if not SEED_FILE.exists():
            return False
        seed_entries = json.loads(SEED_FILE.read_text())
        self._seed_ids = {e.get("catalog_id") for e in seed_entries if e.get("catalog_id")}
        present = {e.get("catalog_id") for e in self.entries}
        changed = False
        for e in seed_entries:
            cid = e.get("catalog_id")
            if cid and cid not in present and cid not in self._removed_seed_ids:
                self.entries.append(e)
                present.add(cid)
                changed = True
        return changed

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=2))

    def all(self) -> list[dict]:
        return list(self.entries)

    def list_by_tier(self) -> dict[str, list[dict]]:
        by: dict[str, list[dict]] = {t: [] for t in TIERS}
        for e in self.entries:
            tier = e.get("tier", "")
            by.setdefault(tier, []).append(e)
        for tier, entries in by.items():
            entries.sort(key=lambda e: (e.get("name", "").lower(), e.get("catalog_id", "")))
        return by

    def add(self, entry: dict) -> dict:
        entry = dict(entry)
        entry["catalog_id"] = uuid.uuid4().hex[:12]
        if entry.get("tier") not in TIERS:
            entry["tier"] = TIERS[0]
        if entry.get("version") is None:
            entry["version"] = 1
        if entry.get("metadata") is None:
            entry["metadata"] = {"tags": "", "modelCategory": "other"}
        if entry.get("environment") is None:
            entry["environment"] = {}
        if entry.get("arguments") is None:
            entry["arguments"] = []
        if entry.get("project") is None:
            entry["project"] = ""
        if entry.get("caching_enabled") is None:
            entry["caching_enabled"] = False
        if entry.get("registry") is None:
            entry["registry"] = None
        self.entries.append(entry)
        self._save()
        return entry

    def remove(self, catalog_id: str) -> bool:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.get("catalog_id") != catalog_id]
        if len(self.entries) < before:
            if catalog_id in self._seed_ids:
                self._removed_seed_ids.add(catalog_id)
                self._write_removed_seeds()
            self._save()
            return True
        return False
