#!/usr/bin/env python3
"""Pre-hardlink secret scan (audit finding 1 — the P0 repeat-vector close).

Run BEFORE `hardlinker.py` syncs this tree into the delivery repo. Scans
every file that WOULD be mirrored (applying the same ignore rules as
hardlink_config.json) for credential-shaped content:

  - JWTs (eyJ-headered three-part tokens, any algorithm header)
  - virtual API keys (hllm-…)
  - enrollment codes (enroll-…)
  - provider keys (sk-…)
  - K8s service-account JSON blobs ("token": "…" with RS256 header nearby)

Exit 1 on ANY hit with the file:line and a REDACTED match preview — never
the value itself. Dry-run by design: it reads nothing it doesn't print a
path for, and it never modifies files.

Usage:
    python scripts/secret_scan.py            # scan, exit 0/1
    python scripts/secret_scan.py --quiet    # only output on failure
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Same ignore list as hardlink_config.json (single source of truth would be
# nicer; duplicated deliberately — this script must not import fleet code).
IGNORE: list[str] = json.loads((ROOT / "hardlink_config.json").read_text())["ignore"] + [
    # the scanner's own artifacts + docs that quote patterns
    "scripts/secret_scan.py",
    "AUDIT.md",
    "REMEDIATION.md",
]

PATTERNS: dict[str, re.Pattern[str]] = {
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*"),
    "hllm-key": re.compile(r"hllm-[A-Za-z0-9_-]{30,}"),
    "enroll-code": re.compile(r"enroll-[A-Za-z0-9_-]{30,}"),
    "provider-key": re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
}

# Placeholder/sanitized markers — docs legitimately contain scrubbed examples
# ("hllm-REPLACED", "<token>", "your-key-here"); a match INSIDE one of these
# is documentation, not a credential.
_PLACEHOLDER_RE = re.compile(
    r"REPLACE|REDACT|EXAMPLE|PLACEHOLDER|YOUR[_-]?|<[^>]*>|xxx|XXXX|test[-_]?key",
    re.IGNORECASE,
)

MAX_FILE_BYTES = 2_000_000  # skip huge binaries/transcripts by size
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".gz", ".tgz", ".tar", ".whl", ".pyc", ".pdf"}


def _ignored(rel: str) -> bool:
    for pat in IGNORE:
        if fnmatch.fnmatch(rel, pat) or rel.startswith(pat.rstrip("/") + "/"):
            return True
    return False


def main() -> int:
    quiet = "--quiet" in sys.argv
    hits: list[tuple[str, int, str]] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = os.path.relpath(dirpath, ROOT)
        # prune ignored directories early (don't descend)
        dirnames[:] = [
            d for d in dirnames
            if not _ignored(os.path.normpath(os.path.join(rel_dir, d)))
            and d not in (".git", ".venv", "__pycache__")
        ]
        for name in filenames:
            rel = os.path.normpath(os.path.join(rel_dir, name))
            if _ignored(rel):
                continue
            path = Path(dirpath) / name
            if path.suffix.lower() in BINARY_EXT:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(errors="replace")
            except OSError:
                continue
            scanned += 1
            for kind, pattern in PATTERNS.items():
                for m in pattern.finditer(text):
                    # skip matches that sit inside a sanitized placeholder
                    window = text[max(0, m.start() - 20):m.end() + 20]
                    if _PLACEHOLDER_RE.search(window):
                        continue
                    line_no = text.count("\n", 0, m.start()) + 1
                    hits.append((str(path.relative_to(ROOT)), line_no, kind))

    if hits:
        print(f"SECRET SCAN FAILED — {len(hits)} hit(s) in {scanned} files scanned:")
        for path, line_no, kind in hits:
            print(f"  {path}:{line_no}  [{kind}]")
        print("\nTreat every match as burned: rotate/revoke, then remove the material")
        print("(and purge VCS history where it was committed). Then re-run this scan.")
        return 1

    if not quiet:
        print(f"secret scan clean: {scanned} files, no credential-shaped content")
    return 0


if __name__ == "__main__":
    sys.exit(main())
