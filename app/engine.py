"""Independent async scheduler over the durable Store queue."""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass

from .core import BudgetExceeded, LeaseLost


@dataclass
class EngineSettings:
    tick_seconds: float = 2.0
    lease_seconds: float = 120.0
    worker_timeout_seconds: float = 240.0
    retry_delay_seconds: float = 30.0
    max_attempts: int = 3
    concurrency: int = 1


class Engine:
    """The worker proposes; Store alone checks and commits workspace changes.

    Provider adapters reserve usage before *each* outbound request. This engine
    never charges again because one research job may make several API calls.
    """

    def __init__(self, store, worker, settings=None):
        self.store = store
        self.worker = worker
        self.settings = settings or EngineSettings()
        self.owner = "engine-" + uuid.uuid4().hex
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._runner = None
        self._loop_ref = None
        self._run_lock = asyncio.Lock()

    def _setting(self, name, default):
        if isinstance(self.settings, dict):
            return self.settings.get(name, default)
        return getattr(self.settings, name, default)

    def _safe_error(self, error):
        sanitizer = self._setting("safe_error", None)
        if callable(sanitizer):
            try:
                return str(sanitizer(error))[:2000]
            except Exception:
                pass
        # Unknown worker exceptions can contain provider credentials or URLs.
        return f"{type(error).__name__}: background work failed"

    @property
    def running(self):
        return self._runner is not None and not self._runner.done()

    async def start(self):
        if self.running:
            return
        self._loop_ref = asyncio.get_running_loop()
        self._stop_event.clear()
        self._runner = asyncio.create_task(self._loop(), name="thinking-helper-scheduler")

    async def stop(self):
        self._stop_event.set()
        self.wake()
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
            self._runner = None

    def wake(self):
        if self._loop_ref is not None and self._loop_ref.is_running():
            self._loop_ref.call_soon_threadsafe(self._wake_event.set)
        else:
            self._wake_event.set()

    async def _loop(self):
        while not self._stop_event.is_set():
            self._wake_event.clear()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.store.add_event(None, "engine.error", {"error": self._safe_error(error)})
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=max(0.05, float(self._setting("tick_seconds", 2.0))))
            except asyncio.TimeoutError:
                pass

    async def run_once(self):
        """Schedule persisted due topics, then execute at most one bounded batch."""
        async with self._run_lock:
            self.store.schedule_due_topics()
            jobs = []
            for _ in range(max(1, int(self._setting("concurrency", 1)))):
                job = self.store.claim_job(
                    self.owner,
                    lease_seconds=float(self._setting("lease_seconds", 120.0)),
                    max_attempts=int(self._setting("max_attempts", 3)),
                )
                if job is None:
                    break
                jobs.append(job)
            if jobs:
                await asyncio.gather(*(self._execute(job) for job in jobs))
            return len(jobs)

    async def _heartbeat(self, job_id):
        lease = float(self._setting("lease_seconds", 120.0))
        while True:
            await asyncio.sleep(min(1.0, max(0.02, lease / 3.0)))
            self.store.renew_lease(job_id, self.owner, lease_seconds=lease)

    async def _execute(self, job):
        worker_task = asyncio.create_task(
            asyncio.wait_for(
                self.worker(job["topic_snapshot"], job),
                timeout=float(self._setting("worker_timeout_seconds", 240.0)),
            )
        )
        heartbeat = asyncio.create_task(self._heartbeat(job["id"]))
        try:
            done, _ = await asyncio.wait((worker_task, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                # Pause/cancel/another owner invalidated the lease: stop spending.
                await heartbeat
            proposal = await worker_task
            self.store.finish_job(job["id"], self.owner, proposal)
        except LeaseLost:
            # Cancellation and stale ownership are already durable user actions.
            pass
        except BudgetExceeded as error:
            with contextlib.suppress(LeaseLost):
                self.store.defer_job(
                    job["id"], self.owner,
                    run_at=self.store.next_budget_reset(),
                    reason=self._safe_error(error), restore_attempt=True,
                )
        except asyncio.CancelledError:
            with contextlib.suppress(LeaseLost):
                self.store.defer_job(
                    job["id"], self.owner, run_at=time.time(),
                    reason="Engine stopped; work can resume after restart", restore_attempt=True,
                )
            raise
        except Exception as error:
            with contextlib.suppress(LeaseLost):
                self.store.fail_job(
                    job["id"], self.owner, self._safe_error(error),
                    max_attempts=int(self._setting("max_attempts", 3)),
                    retry_delay_seconds=float(self._setting("retry_delay_seconds", 30.0)),
                )
        finally:
            for task in (worker_task, heartbeat):
                if not task.done():
                    task.cancel()
            for task in (worker_task, heartbeat):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
