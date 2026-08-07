# AGENTS.md

- Run the downloader with a YAML config: `python model_downloader.py -c <config.yaml>` (model_downloader.py:497).
- Config must include `repo.type` (`hf` or `ngc`) and `repo.id` (e.g., `org/model:version`). Missing fields raise ValueError (model_downloader.py:110‑115).
- NGC token defaults to `NGC_API_KEY` env var; HF token defaults to `HF_TOKEN` env var (model_downloader.py:527‑540).
- Destination `path` can be a local directory or `s3://bucket/...`. S3 triggers temporary local staging (`tempfile.mkdtemp`) (model_downloader.py:542‑547).
- Resume tracking stored under `~/.cache/model-downloader/<hash>/tracker.json`; hash is SHA‑256 of the destination path (model_downloader.py:56‑62).
- Interrupt (Ctrl‑C) sets a global `_interrupted` flag; partial downloads are kept for resume (model_downloader.py:208‑213, 335‑338).
- Upload to S3 preserves relative paths under `org_name/model_name/` prefixes (upload_to_s3:472‑474).
- After successful S3 upload, temporary staging directory is cleaned up (`_cleanup_temp`) (model_downloader.py:590‑603).
- Progress shown via `tqdm` for both download and upload steps.
