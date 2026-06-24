import os
import streamlit as st
import boto3

S3_URL = "http://local-s3-service.ezdata-system.svc.cluster.local:30000"
TOKEN_PATH = "/run/secrets/kubernetes.io/serviceaccount/token"

st.set_page_config(page_title="S3 Browser", layout="wide")


def get_s3():
    kwargs = {"endpoint_url": S3_URL}
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH) as f:
            kwargs["aws_access_key_id"] = f.read().strip()
        kwargs["aws_secret_access_key"] = "s3"
    return boto3.client("s3", **kwargs)


def format_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ── Callbacks (run before the script body on next rerun) ──

def select_bucket(bucket_name: str) -> None:
    st.session_state.selected_bucket = bucket_name
    st.session_state.current_prefix = ""
    st.session_state.upload_message = None
    st.session_state.download_target = None
    st.session_state.pending_delete_file = None


def navigate_to(folder_prefix: str) -> None:
    st.session_state.current_prefix = folder_prefix
    st.session_state.upload_message = None
    st.session_state.download_target = None
    st.session_state.pending_delete_file = None


def go_up() -> None:
    prefix = st.session_state.current_prefix
    parts = prefix.rstrip("/").split("/")
    st.session_state.current_prefix = (
        "/".join(parts[:-1]) + "/" if parts[:-1] else ""
    )
    st.session_state.upload_message = None
    st.session_state.download_target = None
    st.session_state.pending_delete_file = None


def refresh() -> None:
    pass  # a no-op callback to trigger a rerun


def on_file_upload() -> None:
    uploaded = st.session_state.get(
        f"upload_{st.session_state.upload_key_counter}"
    )
    if not uploaded:
        return
    if not isinstance(uploaded, list):
        uploaded = [uploaded]

    bucket = st.session_state.selected_bucket
    prefix = st.session_state.current_prefix
    ok, errors = 0, []

    for f in uploaded:
        key = f"{prefix}{f.name}"
        try:
            get_s3().put_object(Bucket=bucket, Key=key, Body=f.getvalue())
            ok += 1
        except Exception as e:
            errors.append(f"{f.name}: {e}")

    parts = []
    if ok:
        parts.append(f"✓ Uploaded {ok} file(s)")
    if errors:
        parts.append(f"✗ {'; '.join(errors)}")
    st.session_state.upload_message = " | ".join(parts)
    st.session_state.upload_key_counter += 1


def set_download_target(key: str) -> None:
    st.session_state.download_target = key


def request_delete_file(key: str) -> None:
    st.session_state.pending_delete_file = key


def cancel_delete_file() -> None:
    st.session_state.pending_delete_file = None


def confirm_delete_file() -> None:
    key = st.session_state.pending_delete_file
    if not key:
        return
    try:
        get_s3().delete_object(
            Bucket=st.session_state.selected_bucket, Key=key
        )
        st.session_state.upload_message = f"✓ Deleted `{key}`"
    except Exception as e:
        st.session_state.upload_message = f"✗ Delete failed: {e}"
    st.session_state.pending_delete_file = None
    st.rerun()


def create_bucket() -> None:
    bucket_name = st.session_state.get("create_bucket_input", "").strip()
    if not bucket_name:
        return
    try:
        get_s3().create_bucket(Bucket=bucket_name)
        st.session_state.upload_message = f"✓ Created bucket `{bucket_name}`"
        st.session_state.create_bucket_input = ""
    except Exception as e:
        st.session_state.upload_message = f"✗ Create failed: {e}"
    st.rerun()


def request_delete(bucket_name: str) -> None:
    st.session_state.pending_delete_bucket = bucket_name
    st.session_state.upload_message = None


def cancel_delete() -> None:
    st.session_state.pending_delete_bucket = None
    st.session_state.upload_message = None


def confirm_delete(bucket_name: str) -> None:
    confirm_text = st.session_state.get("delete_confirm_input", "")
    if confirm_text != "DELETE":
        st.session_state.upload_message = "✗ Type DELETE to confirm"
        st.session_state.pending_delete_bucket = None
        st.rerun()
        return

    try:
        s3 = get_s3()
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name):
            objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if objects:
                s3.delete_objects(Bucket=bucket_name, Delete={"Objects": objects})

        s3.delete_bucket(Bucket=bucket_name)

        if st.session_state.selected_bucket == bucket_name:
            st.session_state.selected_bucket = None
            st.session_state.current_prefix = ""

        st.session_state.upload_message = f"✓ Deleted bucket `{bucket_name}`"
    except Exception as e:
        st.session_state.upload_message = f"✗ Delete failed: {e}"

    st.session_state.pending_delete_bucket = None
    st.rerun()


# ── Session state initialisation ──

if "selected_bucket" not in st.session_state:
    st.session_state.selected_bucket = None
if "current_prefix" not in st.session_state:
    st.session_state.current_prefix = ""
if "upload_message" not in st.session_state:
    st.session_state.upload_message = None
if "upload_key_counter" not in st.session_state:
    st.session_state.upload_key_counter = 0
if "pending_delete_bucket" not in st.session_state:
    st.session_state.pending_delete_bucket = None
if "download_target" not in st.session_state:
    st.session_state.download_target = None
if "pending_delete_file" not in st.session_state:
    st.session_state.pending_delete_file = None


# ── Sidebar ──

st.sidebar.title("Buckets")
st.sidebar.caption("Click to browse")

s3 = get_s3()

try:
    buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]]
except Exception as e:
    st.sidebar.error(f"Could not list buckets: {e}")
    buckets = []

for bucket in buckets:
    col1, col2 = st.sidebar.columns([3.5, 1])
    col1.button(
        bucket,
        key=f"b_{bucket}",
        use_container_width=True,
        type="primary" if st.session_state.selected_bucket == bucket else "secondary",
        on_click=select_bucket,
        args=(bucket,),
    )
    col2.button(
        "🗑️",
        key=f"del_{bucket}",
        use_container_width=True,
        on_click=request_delete,
        args=(bucket,),
    )

st.sidebar.divider()
st.sidebar.button("🔄 Refresh", use_container_width=True, on_click=refresh)

st.sidebar.divider()
st.sidebar.markdown("### + Create Bucket")
st.sidebar.text_input("Name", key="create_bucket_input")
st.sidebar.button("Create", use_container_width=True, on_click=create_bucket)


# ── Main Area ──

if not st.session_state.selected_bucket:
    st.info("👈 Select a bucket from the sidebar to start browsing.")
    st.stop()

bucket = st.session_state.selected_bucket
prefix = st.session_state.current_prefix

path_display = f"s3://{bucket}/{prefix}"
st.header(path_display)

# ── Upload ──

if st.session_state.upload_message:
    st.success(st.session_state.upload_message)
st.file_uploader(
    f"Upload files to `{path_display}` (drag & drop supported)",
    key=f"upload_{st.session_state.upload_key_counter}",
    on_change=on_file_upload,
    accept_multiple_files=True,
)

st.divider()

# ── Delete confirmation ──

if st.session_state.pending_delete_bucket:
    bucket_to_delete = st.session_state.pending_delete_bucket
    st.error(
        f"⚠️ You are about to delete bucket **`{bucket_to_delete}`** "
        "and **all its contents**. This cannot be undone."
    )
    st.text_input(
        "Type DELETE to confirm:",
        key="delete_confirm_input",
    )
    col1, col2 = st.columns([1, 1])
    col1.button(
        "Confirm Delete",
        type="primary",
        disabled=st.session_state.get("delete_confirm_input", "") != "DELETE",
        on_click=confirm_delete,
        args=(bucket_to_delete,),
        use_container_width=True,
    )
    col2.button("Cancel", on_click=cancel_delete, use_container_width=True)
    st.stop()

# ── File listing ──

try:
    response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")
except Exception as e:
    st.error(f"Error listing objects: {e}")
    st.stop()

common_prefixes = response.get("CommonPrefixes", [])
contents = response.get("Contents", [])

# Header row
cols = st.columns([0.5, 3, 1.2, 0.7, 0.7])
cols[0].markdown("**Type**")
cols[1].markdown("**Name**")
cols[2].markdown("**Size**")
cols[3].markdown("**DL**")
cols[4].markdown("**Del**")
st.divider()

# "Go up" entry
if prefix:
    cols = st.columns([0.5, 3, 1.2, 0.7, 0.7])
    cols[0].markdown("📁")
    cols[1].button("..", key="go_up", on_click=go_up)
    cols[2].markdown("—")
    cols[3].markdown("")
    cols[4].markdown("")

# Sub-folders
for cp in common_prefixes:
    folder_prefix = cp["Prefix"]
    folder_name = folder_prefix[len(prefix):]
    cols = st.columns([0.5, 3, 1.2, 0.7, 0.7])
    cols[0].markdown("📁")
    cols[1].button(folder_name, key=f"f_{folder_prefix}", on_click=navigate_to, args=(folder_prefix,))
    cols[2].markdown("—")
    cols[3].markdown("")
    cols[4].markdown("")

# Files
for obj in contents:
    key = obj["Key"]
    if key == prefix or key.endswith("/"):
        continue
    file_name = key[len(prefix):]
    cols = st.columns([0.5, 3, 1.2, 0.7, 0.7])
    cols[0].markdown("📄")
    cols[1].markdown(file_name)
    cols[2].markdown(format_size(obj["Size"]))
    cols[3].button("⬇️", key=f"dl_{key}", on_click=set_download_target, args=(key,))
    cols[4].button("🗑️", key=f"rm_{key}", on_click=request_delete_file, args=(key,))

if not common_prefixes and not contents:
    st.info("This folder is empty.")

st.divider()

# ── Delete file confirmation ──

if st.session_state.pending_delete_file:
    file_key = st.session_state.pending_delete_file
    st.warning(f"Delete **`{file_key}`**?")
    col1, col2 = st.columns([1, 1])
    col1.button(
        "Confirm Delete",
        type="primary",
        on_click=confirm_delete_file,
        use_container_width=True,
    )
    col2.button("Cancel", on_click=cancel_delete_file, use_container_width=True)


# ── Download area ──

if st.session_state.download_target:
    key = st.session_state.download_target
    try:
        s3_obj = s3.get_object(Bucket=bucket, Key=key)
        file_name = key.rsplit("/", 1)[-1]
        content_type = s3_obj.get("ContentType", "application/octet-stream")
        col1, col2 = st.columns([1, 1])
        col1.download_button(
            f"⬇️ Download `{file_name}`",
            data=s3_obj["Body"].read(),
            file_name=file_name,
            mime=content_type,
        )
        col2.button(
            "Close",
            on_click=lambda: st.session_state.update(download_target=None),
            use_container_width=True,
        )
    except Exception as e:
        st.error(f"Download failed: {e}")
        st.session_state.download_target = None
