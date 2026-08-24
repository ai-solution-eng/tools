"""In-memory download queue with bounded concurrency.

Queued submissions acquire a semaphore slot before creating the k8s Job so at
most `max_concurrency` downloads run at once; the rest wait. On startup the
queue reconciles previously-created Jobs from Kubernetes so downloads submitted
before a restart (or still running) reappear in the UI. State is otherwise held
in-process.
"""

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass, field

from .k8s import K8sClient


@dataclass
class JobRecord:
    id: str
    namespace: str
    model_name: str
    status: str  # queued | running | succeeded | failed
    error: str = ""
    k8s_job_name: str = ""
    hf_secret_name: str = ""
    pvc_url: str = ""
    storage: str = "pvc"  # pvc | s3
    s3_path: str = ""  # user-chosen s3://bucket/prefix/ destination
    cache_root: str = ""  # user-chosen absolute PVC path; empty => /mnt/large-models/{model_name}
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class JobQueue:
    def __init__(
        self,
        max_concurrency: int,
        k8s: K8sClient,
        pvc_name: str,
        container_path: str = "/mnt/models",
        pvc_subpath: str = "large-models",
        s3_bucket: str = "",
        s3_prefix: str = "",
    ):
        self.max_concurrency = max_concurrency
        self.k8s = k8s
        self.pvc_name = pvc_name
        self.container_path = container_path
        self.pvc_subpath = pvc_subpath
        self.s3_bucket = s3_bucket
        self.s3_prefix = s3_prefix.rstrip("/")
        self.jobs: dict[str, JobRecord] = {}
        self._sem: asyncio.Semaphore | None = None

    def _pvc_url(self, model_name: str, cache_root: str = "") -> str:
        if cache_root:
            # The job mounts the PVC at /mnt/. cache_root is an absolute path under
            # that root, so the PVC subpath is cache_root minus the leading /mnt/.
            sub = cache_root.lstrip("/")
            sub = sub.removeprefix("mnt/") if sub.startswith("mnt/") else sub
        else:
            sub = f"{self.pvc_subpath}/{model_name}"
        return f"pvc://{self.pvc_name}/{sub}?containerPath={self.container_path}"

    def _s3_url(self, model_name: str, s3_path: str = "") -> str:
        if s3_path:
            dest = s3_path.rstrip("/")
        else:
            prefix = f"{self.s3_prefix}/" if self.s3_prefix else ""
            dest = f"s3://{self.s3_bucket}/{prefix}".rstrip("/")
        return f"{dest}/{model_name}"

    def _output_url(self, model_name: str, storage: str, s3_path: str = "", cache_root: str = "") -> str:
        if storage == "s3":
            return self._s3_url(model_name, s3_path)
        return self._pvc_url(model_name, cache_root)

    async def start(self) -> None:
        self._sem = asyncio.Semaphore(self.max_concurrency)

    async def reconcile(self) -> None:
        """Rebuild in-memory tracking from Jobs that already exist in k8s."""
        managed = await self.k8s.list_managed_jobs()
        for m in managed:
            if m["id"] in self.jobs:
                continue
            record = JobRecord(
                id=m["id"],
                namespace=m["namespace"],
                model_name=m["model_name"],
                status=m["status"],
                error=m["error"],
                k8s_job_name=m["job_name"],
                storage=m.get("storage", "pvc"),
                s3_path=m.get("s3_path", ""),
                cache_root=m.get("cache_root", ""),
                pvc_url=self._output_url(m["model_name"], m.get("storage", "pvc"), m.get("s3_path", ""), m.get("cache_root", "")),
                created_at=m["created_at"],
                finished_at=m["finished_at"],
            )
            if record.status in ("queued", "running"):
                record.started_at = record.started_at or record.created_at
            self.jobs[record.id] = record
            if record.status == "running":
                asyncio.create_task(self._watch(record))

    async def submit(
        self,
        namespace: str,
        model_name: str,
        hf_token: str,
        storage: str = "pvc",
        s3_path: str = "",
        chat_template_path: str = "",
        chat_template_contents: str = "",
        cache_root: str = "",
    ) -> JobRecord:
        if self._sem is None:
            raise RuntimeError("queue not started")
        job_id = uuid.uuid4().hex[:12]
        record = JobRecord(
            id=job_id,
            namespace=namespace,
            model_name=model_name,
            status="queued",
            storage=storage,
            s3_path=s3_path,
            cache_root=cache_root,
            pvc_url=self._output_url(model_name, storage, s3_path, cache_root),
        )
        self.jobs[job_id] = record
        asyncio.create_task(self._run(record, hf_token, chat_template_path, chat_template_contents))
        return record

    async def _run(
        self, record: JobRecord, hf_token: str, chat_template_path: str = "", chat_template_contents: str = ""
    ) -> None:
        if self._sem is None:
            raise RuntimeError("queue not started")
        async with self._sem:
            record.status = "running"
            record.started_at = time.time()
            try:
                secret_name, job_name = await self.k8s.create_job(
                    record.namespace,
                    record.model_name,
                    hf_token,
                    record.id,
                    storage=record.storage,
                    s3_path=record.s3_path,
                    chat_template_path=chat_template_path,
                    chat_template_contents=chat_template_contents,
                    cache_root=record.cache_root,
                )
                record.hf_secret_name = secret_name
                record.k8s_job_name = job_name
                success, msg = await self.k8s.wait_for_job(record.namespace, job_name)
                record.status = "succeeded" if success else "failed"
                if not success:
                    record.error = msg
            except Exception as e:
                record.status = "failed"
                record.error = f"{type(e).__name__}: {e}"
            finally:
                record.finished_at = time.time()
                if record.hf_secret_name:
                    try:
                        await self.k8s.delete_secret(record.namespace, record.hf_secret_name)
                    except Exception:
                        pass

    async def _watch(self, record: JobRecord) -> None:
        """Poll a reconciled (already-running) Job to completion without semaphore."""
        success, msg = await self.k8s.wait_for_job(record.namespace, record.k8s_job_name)
        record.status = "succeeded" if success else "failed"
        if not success:
            record.error = msg
        record.finished_at = time.time()

    def get(self, job_id: str) -> JobRecord | None:
        return self.jobs.get(job_id)

    async def get_logs(self, job_id: str) -> str:
        record = self.jobs.get(job_id)
        if not record:
            return "(job not found)"
        if not record.k8s_job_name:
            return "(job has not started yet)"
        return await self.k8s.get_job_logs(record.namespace, record.k8s_job_name)

    async def remove(self, job_id: str) -> str | None:
        """Remove a finished job: delete k8s Job + Secret, drop from tracking. Returns error string or None."""
        record = self.jobs.pop(job_id, None)
        if not record:
            return "job not found"
        if record.status not in ("failed", "succeeded"):
            self.jobs[job_id] = record
            return "only finished jobs can be removed"
        if record.k8s_job_name:
            try:
                await self.k8s.delete_job(record.namespace, record.k8s_job_name)
            except Exception as e:
                record.error = f"delete error: {e}"
                self.jobs[job_id] = record
                return f"failed to delete k8s job: {e}"
        if record.hf_secret_name:
            try:
                await self.k8s.delete_secret(record.namespace, record.hf_secret_name)
            except Exception:
                pass
        return None

    async def get_progress(self, job_id: str) -> dict:
        record = self.jobs.get(job_id)
        if not record:
            return {"error": "job not found"}
        if not record.k8s_job_name:
            return {"error": "job has not started yet"}
        if record.status != "running":
            return {"status": record.status, "error": record.error}
        return await self.k8s.check_progress(record.namespace, record.k8s_job_name)

    def list_jobs(self) -> list[JobRecord]:
        return sorted(self.jobs.values(), key=lambda r: r.created_at, reverse=True)
