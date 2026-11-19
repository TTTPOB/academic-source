"""Dispatch only as many papers as can run; publish results in one place."""

from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Condition
from typing import Any

from pydantic import Field

from academic_source.domain import AcquisitionRequest, AcquisitionResult, Job


class ParallelJob(Job):
    """Live status projection; the paper ordinal need not survive a restart."""

    current_paper: int | None = Field(default=None, exclude=True)


@dataclass
class _Batch:
    job: Job
    entries: list[dict[str, Any]]
    config: dict[str, Any]
    done: Future[None] = field(default_factory=Future)
    next_index: int = 0
    active: set[int] = field(default_factory=set)
    results: dict[int, AcquisitionResult] = field(default_factory=dict)
    cancelled: bool = False

    def update_progress(self) -> None:
        self.job.current_identifier = (
            self.entries[min(self.active)]["identifier"] if self.active else ""
        )


class PaperScheduler:
    def __init__(
        self,
        workers: int,
        run_paper: Callable[
            [dict[str, Any], AcquisitionRequest, str, dict[str, Any]], AcquisitionResult
        ],
        save: Callable[[Job], None],
    ) -> None:
        self._workers = workers
        self._run_paper = run_paper
        self._save = save
        self._batches: dict[str, _Batch] = {}
        self._ready: OrderedDict[str, _Batch] = OrderedDict()
        self._running: dict[Future[AcquisitionResult], tuple[_Batch, int]] = {}
        self._last: str | None = None
        self._changed = Condition()
        self._closing = False
        self._papers = ThreadPoolExecutor(workers, thread_name_prefix="paper")
        self._coordinator = ThreadPoolExecutor(1, thread_name_prefix="paper-scheduler")
        self._loop = self._coordinator.submit(self._run)

    def submit(
        self, job: Job, entries: list[dict[str, Any]], config: dict[str, Any]
    ) -> None:
        with self._changed:
            # Surface a stopped coordinator instead of accepting work it cannot run.
            if self._loop.done():
                self._loop.result()
            if self._closing:
                raise RuntimeError("Application is closed")
            self._save(job)
            batch = _Batch(job, entries, config)
            self._batches[job.id] = self._ready[job.id] = batch
            self._changed.notify()

    def status(self, job_id: str) -> Job | None:
        with self._changed:
            batch = self._batches.get(job_id)
            if batch is None:
                return None
            snapshot = ParallelJob.model_validate(
                batch.job, from_attributes=True
            ).model_copy(deep=True)
            if snapshot.status == "queued":
                # Running jobs count as ahead, plus earlier not-yet-started jobs.
                snapshot.queue_ahead = list(self._batches).index(job_id)
            elif batch.active:
                snapshot.current_paper = min(batch.active) + 1
            return snapshot

    def future(self, job_id: str) -> Future[None] | None:
        with self._changed:
            batch = self._batches.get(job_id)
            return batch.done if batch is not None else None

    def cancel(self, job_id: str) -> Job | None:
        with self._changed:
            batch = self._batches.get(job_id)
            if batch is None:
                return None
            batch.cancelled = True
            self._ready.pop(job_id, None)
            if not batch.active:
                self._finish(batch)
                return batch.job.model_copy(deep=True)
            return self.status(job_id)

    def close(self) -> None:
        with self._changed:
            self._closing = True
            self._changed.notify()
        try:
            self._loop.result()
        finally:
            self._coordinator.shutdown()
            self._papers.shutdown()

    def _wake(self, _: Future[AcquisitionResult]) -> None:
        with self._changed:
            self._changed.notify()

    def _run(self) -> None:
        try:
            with self._changed:
                while self._batches or not self._closing:
                    for future in tuple(self._running):
                        if future.done():
                            self._complete(future)
                    self._dispatch()
                    if not self._batches and self._closing:
                        return
                    # A paper can finish before its callback is registered, in
                    # which case the notification runs inline on this thread.
                    if not any(future.done() for future in self._running):
                        self._changed.wait()
        except BaseException as exc:
            with self._changed:
                for batch in self._batches.values():
                    batch.done.set_exception(exc)
            raise

    def _dispatch(self) -> None:
        while self._ready and len(self._running) < self._workers:
            if self._last in self._ready:
                self._ready.move_to_end(self._last)
            job_id, batch = self._ready.popitem(last=False)
            self._last = job_id
            job = batch.job
            if job.status == "queued":
                job.status = "running"
                job.started_at = datetime.now(UTC).isoformat()
            index = batch.next_index
            batch.next_index += 1
            batch.active.add(index)
            if batch.next_index < job.total:
                self._ready[job_id] = batch
            batch.update_progress()
            self._save(job)
            future = self._papers.submit(
                self._run_paper, batch.entries[index], job.request, job_id, batch.config
            )
            self._running[future] = (batch, index)
            future.add_done_callback(self._wake)

    def _complete(self, future: Future[AcquisitionResult]) -> None:
        batch, index = self._running.pop(future)
        batch.active.remove(index)
        batch.results[index] = future.result()
        job = batch.job
        job.results = [batch.results[index] for index in sorted(batch.results)]
        job.completed = len(job.results)
        if not batch.active and (batch.cancelled or job.completed == job.total):
            self._finish(batch)
        else:
            batch.update_progress()
            self._save(job)

    def _finish(self, batch: _Batch) -> None:
        job = batch.job
        if batch.cancelled:
            job.status = "cancelled"
        else:
            successes = sum(item.status == "succeeded" for item in job.results)
            job.status = (
                "succeeded"
                if successes == job.total
                else "partial"
                if successes
                else "failed"
            )
        job.current_identifier = ""
        self._save(job)
        del self._batches[job.id]
        batch.done.set_result(None)
