"""Concurrency control: a bounded pool of sandbox runs.

At most MAX_CONCURRENT_JOBS runs hold a slot at once, and a run only creates
its Job after acquiring one, so this is also the cap on in-flight Jobs from
this process. Runs are persisted as `queued` before they are submitted, so a
crash loses nothing: recover() picks them up on the next start.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from app import db, executor

log = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT_JOBS = 4


def max_concurrent_from_env() -> int:
    try:
        return max(1, int(os.getenv("MAX_CONCURRENT_JOBS", DEFAULT_MAX_CONCURRENT_JOBS)))
    except ValueError:
        return DEFAULT_MAX_CONCURRENT_JOBS


class WorkerPool:
    def __init__(self, max_concurrent: int | None = None):
        self.max_concurrent = max_concurrent or max_concurrent_from_env()
        self._slots = asyncio.Semaphore(self.max_concurrent)
        self._tasks: set[asyncio.Task] = set()
        self.in_flight = 0
        self.peak_in_flight = 0

    def stats(self) -> dict:
        return {
            "max_concurrent": self.max_concurrent,
            "in_flight": self.in_flight,
            "waiting": len(self._tasks) - self.in_flight,
            "peak_in_flight": self.peak_in_flight,
        }

    def submit(self, run_id: str, code: str, tests: str) -> asyncio.Task:
        """Queue an already-persisted `queued` run. Returns immediately."""
        return self._spawn(self._execute(run_id, code, tests), run_id)

    def _spawn(self, coro, run_id: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=f"run-{run_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _execute(self, run_id: str, code: str, tests: str) -> None:
        async with self._slot():
            await executor.execute_run(run_id, code, tests)

    async def _attach(self, run_id: str, job_name: str, started_at: str | None) -> None:
        async with self._slot():
            await executor.attach_run(run_id, job_name, started_at)

    @asynccontextmanager
    async def _slot(self):
        async with self._slots:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            try:
                yield
            finally:
                self.in_flight -= 1

    async def recover(self) -> dict:
        """Re-enqueue runs left `queued`/`running` by a previous process."""
        summary = {"requeued": 0, "reattached": 0, "stale": 0}
        rows = await db.async_call(db.get_queued_or_running_runs)
        for row in rows:
            if row["status"] == "queued":
                self.submit(row["id"], row["code"], row["tests"])
                summary["requeued"] += 1
                continue

            try:
                exists = await executor.job_exists(row["job_name"])
            except Exception as exc:
                # Can't tell whether the Job survived; attaching will record infra_error if it didn't.
                log.warning("recover: could not check job %s: %s", row["job_name"], exc)
                exists = True

            if exists:
                self._spawn(self._attach(row["id"], row["job_name"], row["started_at"]), row["id"])
                summary["reattached"] += 1
            else:
                await db.async_call(
                    db.update_run,
                    row["id"],
                    status="infra_error",
                    stderr=f"server restarted and job {row['job_name'] or '(never created)'} no longer exists",
                    finished_at=executor._now(),
                )
                summary["stale"] += 1

        if rows:
            log.info("recover: %s", summary)
        return summary

    async def join(self, tasks: list[asyncio.Task]) -> None:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        """Cancel in-flight work. Rows stay `queued`/`running` and are recovered on next start."""
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
