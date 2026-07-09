# model-downloader

A command-line tool for downloading AI models from **HuggingFace** or **NVIDIA NGC** into a local directory or an S3 bucket (including S3-compatible storage like MinIO). It supports resuming interrupted downloads, shows progress bars, and is fully configured via a single YAML file.

## What it does

- Downloads complete model snapshots from HuggingFace Hub (`huggingface_hub.snapshot_download`) or the NVIDIA NGC catalog REST API.
- Writes files to a local directory or uploads them to an S3 bucket, preserving the `org/model/...` relative path structure.
- Resumes interrupted downloads: per-file state is tracked in `~/.cache/model-downloader/<hash>/tracker.json`, where `<hash>` is the first 16 chars of the SHA-256 of the destination path. Partial files are kept as `*.part` and continued on the next run using HTTP `Range` requests (NGC) or `resume_download` (HF).
- Handles `Ctrl-C` gracefully via a signal handler (model_downloader.py:208-213). Completed files are retained; remaining files resume on the next run. Exit code is `130` when interrupted before any file completes.
- Authenticates to S3 via explicit credentials, an AWS profile, or the default boto3 credential chain.

## Requirements

Python 3.10+ with the packages listed in `requirements.txt`:

```
requests>=2.31.0
huggingface_hub>=0.23.0
boto3>=1.34.0
tqdm>=4.66.0
pyyaml>=6.0
```

Install them with:

```bash
pip install -r requirements.txt
```

## Configuration

All parameters are read from a YAML file passed with `-c`. The schema:

```yaml
repo:
  type: hf | ngc              # required
  id: org/model[:version]     # required (NGC versions: "org/model:1.0" or "org/team/model:1.0")
  token: "..."                # optional; falls back to HF_TOKEN (hf) or NGC_API_KEY (ngc) env vars

destination:
  path: /local/dir | s3://bucket/prefix   # required

s3:                           # optional, only used when destination.path starts with s3://
  endpoint_url: "..."         # optional; for S3-compatible storage (e.g. MinIO)
  region: "..."               # optional
  access_key_id: "..."        # optional; overrides default chain (requires secret_access_key)
  secret_access_key: "..."    # required if access_key_id is set
  session_token: "..."        # optional
  profile: "..."              # optional; overrides explicit keys (cannot be combined with access_key_id)
```

Validation rules enforced in `_load_config` (model_downloader.py:84-128):

- `repo.type` must be `hf` or `ngc`.
- `repo.id` and `destination.path` are required.
- `s3.secret_access_key` is required when `s3.access_key_id` is set.
- `s3.profile` and `s3.access_key_id` are mutually exclusive.

## How to run

```bash
python model_downloader.py -c <config.yaml>
```

The only CLI argument is the path to the configuration file. NGC requires an API key (via `repo.token` or `NGC_API_KEY`); HuggingFace uses `repo.token` or `HF_TOKEN` (public models work without a token).

When the destination is `s3://`, files are first staged in a temporary directory (`tempfile.mkdtemp`) and uploaded after the download step. On successful upload the staging directory is cleaned up (model_downloader.py:590-603).

## Example

Download `google/gemma-3-1b-it` from HuggingFace into an S3-compatible bucket:

`gemma.yaml`:
```yaml
repo:
  type: hf
  id: google/gemma-3-1b-it
  # token: hf_xxx   # optional; or export HF_TOKEN

destination:
  path: s3://models/

s3:
  endpoint_url: "http://local-s3-service.example:30000"
  access_key_id: AKIAIOSFODNN7EXAMPLE
  secret_access_key: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
```

Run:
```bash
export HF_TOKEN=hf_xxx        # only if the repo requires auth
python model_downloader.py -c gemma.yaml
```

Files are uploaded to `s3://models/google/gemma-3-1b-it/<relative-path>`. Re-running the command resumes any partially downloaded files from where it left off.

For NGC, use `type: ngc` and an `id` such as `org/model:version` (omit `:version` to resolve the latest). Set `NGC_API_KEY` or provide `repo.token`.
