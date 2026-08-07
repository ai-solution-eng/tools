"""PVC-backed model catalog with GPU-tier grouping.

The catalog is a JSON file on a small dedicated PVC.  On first start, if
the file is missing, it is seeded from seed_catalog.json bundled in the
image.  Each entry is a full packaged_models config plus a catalog_id
(uuid) and a tier (h200 | rtx-pro-6000 | l40s).
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
        self.entries: list[dict] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            self.entries = json.loads(self.path.read_text())
        elif SEED_FILE.exists():
            self.entries = json.loads(SEED_FILE.read_text())
            self._save()
        else:
            self.entries = []

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
            self._save()
            return True
        return False
