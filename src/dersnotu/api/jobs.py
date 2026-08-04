"""İş kuyruğu ve olay yayını.

`JobStore` kasıtlı olarak dar tutuldu: sadece `create / get / publish /
subscribe`. Bugünkü uygulama süreç içi (tek makine, thread havuzu); Redis +
ARQ'ya geçmek bu arayüzü yeniden yazmak demek, çağıran kodu değiştirmek değil.

Boru hattı bloklayıcı (PDF ayrıştırma, HTTP çağrıları) olduğu için bir thread
havuzunda koşar; olaylar `call_soon_threadsafe` ile asyncio tarafına aktarılır.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Event:
    """SSE'ye giden tek olay."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "at": self.at, **self.data}


@dataclass
class Job:
    id: str
    params: dict[str, Any]
    status: JobStatus = JobStatus.QUEUED
    events: list[Event] = field(default_factory=list)
    error: str | None = None
    pdf_path: Path | None = None
    md_path: Path | None = None
    # Uygulama içi okuyucunun sunduğu dosya (PDF ile aynı render'dan).
    html_path: Path | None = None
    # Yeniden deneme bu dosyadan besleniyor (konu kartları + hizalama içerir).
    doc_path: Path | None = None
    failed_sections: list[int] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    # Kuyrukta bekleme süresi işin süresi değil; sayaç RUNNING'de başlar.
    # Bu ölçüm süre tahminini kalibre ediyor (bkz. estimate.py).
    started_at: float | None = None

    @property
    def elapsed(self) -> float:
        return 0.0 if self.started_at is None else time.monotonic() - self.started_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "error": self.error,
            "created_at": self.created_at,
            "usage": self.usage,
            "has_pdf": bool(self.pdf_path and self.pdf_path.exists()),
            "failed_sections": self.failed_sections,
            # Yeniden deneme ancak doküman diskteyse ve hatalı bölüm varsa
            # anlamlı; arayüz düğmeyi buna bakarak gösteriyor.
            "can_retry": bool(
                self.failed_sections and self.doc_path and self.doc_path.exists()
            ),
            "params": {k: v for k, v in self.params.items() if k != "paths"},
        }


class JobStore:
    """Süreç içi iş deposu + olay yayını.

    Redis'e geçerken: `_jobs` → Redis hash, `_subscribers` → Redis pub/sub,
    `submit` → ARQ enqueue. Çağıran taraf (server.py) değişmez.
    """

    def __init__(self, max_workers: int = 2):
        self._jobs: dict[str, Job] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._max_workers = max_workers
        self._pool: ThreadPoolExecutor | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _ensure_pool(self) -> ThreadPoolExecutor:
        """Havuzu tembel kurar ve kapanmışsa yeniden açar.

        Store modül seviyesinde tekil; lifespan birden çok kez çalışabilir
        (uvicorn --reload, testlerde ardışık TestClient). Kapanmış havuza iş
        vermek "cannot schedule new futures after shutdown" hatası veriyordu.
        """
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=self._max_workers, thread_name_prefix="dersnotu"
            )
        return self._pool

    # ----- iş yaşam döngüsü ---------------------------------------------
    def create(self, params: dict[str, Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], params=params)
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def submit(self, job: Job, fn: Callable[[Job, Callable[[Event], None]], None]) -> None:
        """İşi thread havuzuna verir. `fn` olay yayınlamak için bir callback alır."""

        def runner() -> None:
            job.status = JobStatus.RUNNING
            job.started_at = time.monotonic()
            self.publish(job.id, Event("job:running"))
            try:
                fn(job, lambda ev: self.publish(job.id, ev))
                job.status = JobStatus.DONE
                self.publish(job.id, Event("job:done", job.to_dict()))
            except Exception as exc:  # işi öldür, sunucuyu değil
                job.status = JobStatus.FAILED
                job.error = f"{type(exc).__name__}: {exc}"
                self.publish(job.id, Event("job:failed", {"error": job.error}))
            finally:
                self.publish(job.id, Event("stream:end"))

        self._ensure_pool().submit(runner)

    # ----- olay yayını ---------------------------------------------------
    def publish(self, job_id: str, event: Event) -> None:
        """Thread-safe. Boru hattı thread'inden de çağrılabilir."""
        job = self._jobs.get(job_id)
        if job is None:
            return
        # Delta olayları çok sık; geçmişte tutmak belleği şişirir.
        if event.type != "section:delta":
            job.events.append(event)

        queues = list(self._subscribers.get(job_id, []))
        if not queues or self._loop is None:
            return

        def deliver() -> None:
            for q in queues:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass  # yavaş istemci akışı bloklamasın

        try:
            self._loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            pass  # döngü kapanıyor

    def subscribe(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subscribers.setdefault(job_id, []).append(q)
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(job_id)
        if subs and q in subs:
            subs.remove(q)
        if subs is not None and not subs:
            self._subscribers.pop(job_id, None)

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None  # sonraki submit yeni havuz açar
