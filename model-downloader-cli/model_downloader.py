import argparse
import json
import os
import signal
import sys
import tempfile
import time
import hashlib
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
import yaml
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
NGC_API_BASE = "https://api.ngc.nvidia.com/v2"
CACHE_DIR = Path.home() / ".cache" / "model-downloader"
TRACKER_FILENAME = "tracker.json"
PART_SUFFIX = ".part"

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _parse_ngc_repo_id(repo_id: str):
    """Parse 'org/team/model:version' or 'org/model:version' into components."""
    version = None
    if ":" in repo_id:
        repo_id, version = repo_id.rsplit(":", 1)

    parts = repo_id.split("/")
    if len(parts) == 3:
        org, team, model = parts
    elif len(parts) == 2:
        org, model = parts
        team = None
    else:
        raise ValueError(
            f"Invalid NGC repo_id '{repo_id}'. Expected 'org/team/model:version' "
            "or 'org/model:version'"
        )
    return org, team, model, version


def _resolve_dest(dest: str) -> tuple[str, bool]:
    """Return (resolved_path_or_tempdir, is_s3)."""
    if dest.startswith("s3://"):
        return dest, True
    return os.path.abspath(dest), False


def _tracker_path(local_dest: str) -> Path:
    """Deterministic tracker path based on local dest."""
    return (
        Path(CACHE_DIR)
        / hashlib.sha256(local_dest.encode()).hexdigest()[:16]
        / TRACKER_FILENAME
    )


def _download_url(retries: int = 3, backoff: float = 1.0, **kwargs):
    """Wrapper around requests.get with retries."""
    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.get(**kwargs, stream=True, timeout=(10, 60))
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
    raise last_exc


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config(path: str) -> dict:
    """Load and validate the YAML configuration file.

    Expected schema (all optional unless marked required):
        repo:
          type:  "hf" | "ngc"           # required
          id:    "org/model:version"     # required
          token: "..."                   # optional; falls back to env var
        destination:
          path:  "/local/path" | "s3://bucket/prefix"   # required
        s3:
          endpoint_url: "..."            # optional; for S3-compatible storage
          region:       "..."            # optional
          access_key_id:     "..."       # optional; overrides default chain
          secret_access_key: "..."       # required if access_key_id set
          session_token:     "..."       # optional
          profile:    "..."              # optional; overrides explicit keys
    """
    with open(path) as f:
        cfg = yaml.safe_load(f)

    if not isinstance(cfg, dict):
        raise ValueError("Config file must contain a YAML mapping")

    repo = cfg.get("repo")
    if not isinstance(repo, dict):
        raise ValueError("Missing 'repo' section in config")
    if repo.get("type") not in ("hf", "ngc"):
        raise ValueError("repo.type must be 'hf' or 'ngc'")
    if not repo.get("id"):
        raise ValueError("repo.id is required")

    dest = cfg.get("destination")
    if not isinstance(dest, dict):
        raise ValueError("Missing 'destination' section in config")
    if not dest.get("path"):
        raise ValueError("destination.path is required")

    s3 = cfg.get("s3", {})
    if isinstance(s3, dict) and s3.get("access_key_id") and not s3.get("secret_access_key"):
        raise ValueError("s3.secret_access_key is required when s3.access_key_id is set")
    if isinstance(s3, dict) and s3.get("profile") and s3.get("access_key_id"):
        raise ValueError("Cannot set both s3.profile and s3.access_key_id; pick one")

    return cfg


# ---------------------------------------------------------------------------
# Resume Tracker
# ---------------------------------------------------------------------------

class ResumeTracker:
    """Persistent state for resuming interrupted downloads.

    JSON schema:
    {
      "repo_type": "...",
      "repo_id": "...",
      "version": "...",
      "files": {
        "relative/path/file.bin": {
          "size": 12345678,
          "etag": "...",
          "status": "completed"  | "uploaded"
        }
      }
    }
    """

    def __init__(self, tracker_path: Path):
        self.path = tracker_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = self._load()

    # ---- public helpers ------------------------------------------------

    def is_complete(self, rel_path: str, size: int) -> bool:
        entry = self._data.get("files", {}).get(rel_path)
        if entry is None:
            return False
        return entry.get("status") in ("completed", "uploaded") and entry.get("size") == size

    def is_uploaded(self, rel_path: str) -> bool:
        entry = self._data.get("files", {}).get(rel_path)
        return entry is not None and entry.get("status") == "uploaded"

    def mark_completed(self, rel_path: str, size: int, etag: str = ""):
        self._ensure_files()
        self._data["files"][rel_path] = {"size": size, "etag": etag, "status": "completed"}
        self._flush()

    def mark_uploaded(self, rel_path: str):
        entry = self._data.get("files", {}).get(rel_path)
        if entry:
            entry["status"] = "uploaded"
            self._flush()

    def set_version(self, version: str):
        self._data["version"] = version
        self._flush()

    # ---- internal ------------------------------------------------------

    def _load(self) -> dict:
        try:
            with open(self.path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"repo_type": "", "repo_id": "", "version": "", "files": {}}

    def _ensure_files(self):
        self._data.setdefault("files", {})

    def _flush(self):
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._data, f, indent=2)
        tmp.rename(self.path)


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

_interrupted = False

def _signal_handler(sig, frame):
    global _interrupted
    _interrupted = True


# ---------------------------------------------------------------------------
# NGC Downloader
# ---------------------------------------------------------------------------

class NGCDownloader:
    """Download a model from the NVIDIA NGC catalog with resume support."""

    def __init__(self, repo_id: str, dest: str, token: str, tracker: ResumeTracker):
        org, team, model, version = _parse_ngc_repo_id(repo_id)
        self.org = org
        self.team = team
        self.model = model
        self.version = version
        self.dest = dest
        self.token = token
        self.tracker = tracker

        self._headers = {"Authorization": f"Bearer {token}"}

    def run(self):
        """Entry point — list files, download all, return list of local paths."""
        files = self._list_files()
        local_files = []
        for file_info in tqdm(files, desc="Files", unit="file", position=0):
            if _interrupted:
                break
            rel_path = file_info["path"]
            size = file_info["size"]
            fpath = self._download_file(rel_path, size)
            if fpath:
                local_files.append(fpath)
        return local_files

    # ---- file listing --------------------------------------------------

    def _list_files(self) -> list[dict]:
        if not self.version:
            self.version = self._resolve_latest_version()

        url = f"{NGC_API_BASE}/models/{self.org}"
        if self.team:
            url += f"/{self.team}"
        url += f"/{self.model}/versions/{self.version}"

        resp = _download_url(url, headers=self._headers)
        data = resp.json()

        version_data = data if "files" in data else data.get("modelVersion", data)
        raw_files = version_data.get("files", [])
        return sorted(raw_files, key=lambda f: f["path"])

    def _resolve_latest_version(self) -> str:
        url = f"{NGC_API_BASE}/models/{self.org}"
        if self.team:
            url += f"/{self.team}"
        url += f"/{self.model}/versions"

        resp = _download_url(url, headers=self._headers)
        data = resp.json()
        versions = data.get("modelVersions", data.get("versions", []))
        if not versions:
            raise RuntimeError(f"No versions found for NGC model {self.org}/{self.model}")
        latest = versions[-1]["versionId"]
        self.tracker.set_version(latest)
        return latest

    # ---- file download with resume -------------------------------------

    def _download_file(self, rel_path: str, size: int) -> Optional[str]:
        dest_file = os.path.join(self.dest, rel_path)
        part_file = dest_file + PART_SUFFIX

        if self.tracker.is_complete(rel_path, size):
            return dest_file

        os.makedirs(os.path.dirname(dest_file), exist_ok=True)

        base = f"{NGC_API_BASE}/models/{self.org}"
        if self.team:
            base += f"/{self.team}"
        url = f"{base}/{self.model}/versions/{self.version}/{rel_path}"

        resume_at = 0
        if os.path.exists(part_file):
            resume_at = os.path.getsize(part_file)
            if resume_at >= size:
                os.rename(part_file, dest_file)
                self.tracker.mark_completed(rel_path, size)
                return dest_file

        mode = "ab" if resume_at > 0 else "wb"
        headers = {**self._headers}
        if resume_at > 0:
            headers["Range"] = f"bytes={resume_at}-"

        desc = rel_path.split("/")[-1][:50]
        with _download_url(url, headers=headers) as r, open(part_file, mode) as f:
            if resume_at > 0 and r.status_code == 206:
                pass
            elif resume_at == 0 and r.status_code == 200:
                pass
            elif resume_at > 0 and r.status_code == 200:
                f.seek(0)
                f.truncate(0)
                resume_at = 0

            total = size
            downloaded = resume_at
            chunk_size = 8 * 1024 * 1024

            pbar = tqdm(
                total=total,
                initial=downloaded,
                unit="B",
                unit_scale=True,
                desc=desc,
                position=1,
                leave=False,
            )
            for chunk in r.iter_content(chunk_size=chunk_size):
                if _interrupted:
                    pbar.close()
                    return None
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    pbar.update(len(chunk))
            pbar.close()

        os.rename(part_file, dest_file)
        self.tracker.mark_completed(rel_path, size, etag=r.headers.get("ETag", ""))
        return dest_file


# ---------------------------------------------------------------------------
# HuggingFace Downloader
# ---------------------------------------------------------------------------

def download_huggingface(
    repo_id: str,
    local_dest: str,
    token: Optional[str],
    tracker: ResumeTracker,
) -> list[str]:
    """Download a model from HuggingFace Hub using snapshot_download."""
    from huggingface_hub import snapshot_download, HfApi

    api = HfApi(token=token)
    repo_files = api.list_repo_tree(repo_id, repo_type="model", recursive=True)
    remote_files = [
        f for f in repo_files
        if hasattr(f, "size") and f.size is not None and f.size > 0
    ]

    files_to_download = []
    for rf in remote_files:
        rel_path = rf.path if hasattr(rf, "path") else str(rf)
        size = rf.size
        if not tracker.is_complete(rel_path, size):
            files_to_download.append((rel_path, size))

    if not files_to_download:
        tqdm.write(f"All {len(remote_files)} files already downloaded. Skipping.")
        return [
            os.path.join(local_dest, rf.path if hasattr(rf, "path") else str(rf))
            for rf in remote_files
        ]

    tqdm.write(f"Downloading {len(files_to_download)}/{len(remote_files)} files from {repo_id}")

    snapshot_download(
        repo_id=repo_id,
        local_dir=local_dest,
        local_dir_use_symlinks=False,
        resume_download=True,
        token=token,
        tqdm_class=tqdm,
        ignore_patterns=[],
    )

    for rel_path, size in files_to_download:
        if not _interrupted:
            tracker.mark_completed(rel_path, size)

    result = []
    for root, dirs, files in os.walk(local_dest):
        for fn in files:
            if fn.endswith(PART_SUFFIX):
                continue
            result.append(os.path.join(root, fn))
    return result


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------

def _build_s3_client(s3_config: dict):
    """Create a boto3 S3 client using the provided config or default chain.

    Auth precedence:
      1. Explicit access_key_id / secret_access_key in config
      2. AWS profile name in config
      3. boto3 default chain (env vars, IAM role, ~/.aws/credentials)
    """
    import boto3

    client_kwargs = {}
    if s3_config.get("endpoint_url"):
        client_kwargs["endpoint_url"] = s3_config["endpoint_url"]
    if s3_config.get("region"):
        client_kwargs["region_name"] = s3_config["region"]

    if s3_config.get("profile"):
        session = boto3.Session(profile_name=s3_config["profile"])
        return session.client("s3", **client_kwargs)

    if s3_config.get("access_key_id"):
        return boto3.client(
            "s3",
            aws_access_key_id=s3_config["access_key_id"],
            aws_secret_access_key=s3_config["secret_access_key"],
            aws_session_token=s3_config.get("session_token") or None,
            **client_kwargs,
        )

    return boto3.client("s3", **client_kwargs)


def upload_to_s3(
    file_paths: list[str],
    s3_dest: str,
    local_prefix: str,
    s3_config: Optional[dict] = None,
    org_name: str = "",
    model_name: str = "",
):
    """Upload files to S3, preserving relative paths.

    s3_dest:      s3://bucket/
    local_prefix: the local root from which relative paths are computed.
    s3_config:    optional dict with endpoint_url, region, credentials.
    org_name:     organization name used as top-level S3 prefix.
    model_name:   model name used as second-level S3 prefix.
    """
    from boto3.s3.transfer import TransferConfig

    s3 = _build_s3_client(s3_config or {})
    parsed = urlparse(s3_dest)
    bucket = parsed.netloc

    config = TransferConfig(multipart_threshold=50 * 1024 * 1024)

    for fpath in tqdm(file_paths, desc="Uploading to S3", unit="file", position=0):
        if _interrupted:
            break
        rel = os.path.relpath(fpath, local_prefix)
        s3_key = f"{org_name}/{model_name}/{rel}"
        s3_key = s3_key.lstrip("/")
        file_size = os.path.getsize(fpath)

        desc = rel.split("/")[-1][:50]
        with tqdm(
            total=file_size, unit="B", unit_scale=True, desc=desc, position=1, leave=False
        ) as pbar:

            def _cb(bytes_transferred):
                pbar.update(bytes_transferred - pbar.n)

            s3.upload_file(
                fpath,
                bucket,
                s3_key,
                Config=config,
                Callback=_cb,
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download AI models from HuggingFace or NVIDIA NGC to a local "
                    "directory or S3 bucket with resume support.\n\n"
                    "All parameters are read from a YAML config file.",
    )
    parser.add_argument(
        "-c", "--config",
        required=True,
        help="Path to YAML configuration file",
    )
    args = parser.parse_args()

    # ---- load & validate config ----------------------------------------
    try:
        cfg = _load_config(args.config)
    except (FileNotFoundError, yaml.YAMLError, ValueError) as exc:
        print(f"Error loading config: {exc}", file=sys.stderr)
        sys.exit(1)

    repo_cfg = cfg["repo"]
    dest_cfg = cfg["destination"]
    s3_cfg = cfg.get("s3", {})

    repo_type = repo_cfg["type"]
    repo_id = repo_cfg["id"]
    dest_path = dest_cfg["path"]

    # ---- resolve token -------------------------------------------------
    token = repo_cfg.get("token")
    if repo_type == "ngc":
        if not token:
            token = os.environ.get("NGC_API_KEY")
        if not token:
            print(
                "Error: NGC API key required. Set in config as repo.token "
                "or via NGC_API_KEY env var.",
                file=sys.stderr,
            )
            sys.exit(1)
    elif repo_type == "hf":
        if not token:
            token = os.environ.get("HF_TOKEN")

    # ---- resolve destination -------------------------------------------
    dest, is_s3 = _resolve_dest(dest_path)
    if is_s3:
        local_root = tempfile.mkdtemp(prefix="model-downloader-")
    else:
        local_root = dest
        os.makedirs(local_root, exist_ok=True)

    # ---- setup tracker & signal handler --------------------------------
    tracker_path = _tracker_path(dest_path)
    tracker = ResumeTracker(tracker_path)
    signal.signal(signal.SIGINT, _signal_handler)

    tqdm.write(f"Config:     {args.config}")
    tqdm.write(f"Repo:       {repo_type}  {repo_id}")
    tqdm.write(f"Dest:       {dest}")
    tqdm.write(f"Tracker:    {tracker_path}")

    try:
        # ---- download step ---------------------------------------------
        if repo_type == "ngc":
            downloader = NGCDownloader(repo_id, local_root, token, tracker)
            local_files = downloader.run()
        else:
            local_files = download_huggingface(repo_id, local_root, token, tracker)

        if _interrupted and not local_files:
            tqdm.write("\nInterrupted before any files completed. Partial files remain for resume.")
            sys.exit(130)

        if _interrupted:
            tqdm.write(
                f"\nInterrupted. {len(local_files)} files completed, "
                f"remaining will resume next run."
            )

        # ---- upload step (S3 only) -------------------------------------
        if is_s3 and local_files:
            org_name = repo_id.split("/")[0]
            model_name = repo_id.rsplit(":", 1)[0].rsplit("/", 1)[-1]
            bucket = urlparse(dest).netloc
            tqdm.write(f"Uploading {len(local_files)} files to s3://{bucket}/{org_name}/{model_name}/")
            upload_to_s3(local_files, dest, local_root, s3_config=s3_cfg, org_name=org_name, model_name=model_name)

            for fpath in local_files:
                rel = os.path.relpath(fpath, local_root)
                tracker.mark_uploaded(rel)

            if not _interrupted:
                _cleanup_temp(local_root)

        if not _interrupted:
            tqdm.write("Done — model download completed successfully.")

    except Exception as exc:
        tqdm.write(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)


def _cleanup_temp(path: str):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    main()
