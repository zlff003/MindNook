from __future__ import annotations

import threading
import time
import logging
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from app.core.config import Settings
from app.models import ToolJob
from app.services.jobs import (
    ALERT_JOB_TYPES,
    CASE_JOB_TYPES,
    EXCEL_JOB_TYPES,
    claim_due_jobs,
    execute_tool_job,
    mark_job_failure,
    mark_job_success,
    process_pending_jobs,
    recover_stale_jobs,
    reschedule_rate_limited_job,
    worker_identity,
)
from app.tools.operations import ToolOperations


logger = logging.getLogger(__name__)


class InlineToolDispatcher:
    """Deterministic dispatcher used by tests and standalone harness checks."""

    def __init__(self, session_factory, settings: Settings | str):
        self.session_factory = session_factory
        self.settings = (
            settings
            if isinstance(settings, Settings)
            else Settings(app_env="test", excel_ledger_path=settings)
        )

    def start(self) -> None:
        recover_stale_jobs(
            self.session_factory,
            self.settings.tool_worker_running_timeout_seconds,
            recover_all=True,
        )

    def dispatch(self) -> None:
        process_pending_jobs(self.session_factory, self.settings)

    def close(self) -> None:
        return None


class _PerMinuteRateLimiter:
    def __init__(self, limit: int):
        self.limit = max(0, limit)
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire_or_delay(self) -> float:
        """Reserve a slot, or return the seconds until a slot is available."""
        if self.limit == 0:
            return 0.0
        now = time.monotonic()
        with self._lock:
            while self._timestamps and self._timestamps[0] <= now - 60.0:
                self._timestamps.popleft()
            if len(self._timestamps) < self.limit:
                self._timestamps.append(now)
                return 0.0
            return max(0.1, 60.0 - (now - self._timestamps[0]))


class ThreadedToolDispatcher:
    """Polling worker for durable tool jobs; dispatch() never runs tools inline."""

    def __init__(self, session_factory, settings: Settings | str):
        self.session_factory = session_factory
        self.settings = (
            settings
            if isinstance(settings, Settings)
            else Settings(excel_ledger_path=settings)
        )
        settings = self.settings
        self.operations = ToolOperations(session_factory, settings)
        self.worker_id = worker_identity()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._started = False
        self._start_lock = threading.Lock()
        self._in_flight: set[int] = set()
        self._in_flight_lock = threading.Lock()
        self._alert_limiter = _PerMinuteRateLimiter(
            settings.alert_rate_limit_per_minute
        )
        self._excel_executor = ThreadPoolExecutor(
            max_workers=max(1, settings.tool_worker_excel_workers),
            thread_name_prefix="mindnook-excel",
        )
        self._case_executor = ThreadPoolExecutor(
            max_workers=max(1, settings.tool_worker_case_workers),
            thread_name_prefix="mindnook-case",
        )
        self._alert_executor = ThreadPoolExecutor(
            max_workers=max(1, settings.tool_worker_alert_workers),
            thread_name_prefix="mindnook-alert",
        )
        self._poller = threading.Thread(
            target=self._poll_loop,
            name="mindnook-tool-dispatcher",
            daemon=True,
        )

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            recover_stale_jobs(
                self.session_factory,
                self.settings.tool_worker_running_timeout_seconds,
                recover_all=True,
            )
            self._started = True
            self._poller.start()

    def dispatch(self) -> None:
        """Wake the poller after the enqueue transaction commits."""
        self._wake.set()

    def _executor_for(self, job_type: str) -> ThreadPoolExecutor:
        if job_type in EXCEL_JOB_TYPES:
            return self._excel_executor
        if job_type in CASE_JOB_TYPES:
            return self._case_executor
        if job_type in ALERT_JOB_TYPES:
            return self._alert_executor
        return self._case_executor

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._claim_and_submit()
            except Exception:  # noqa: BLE001 - a poll failure must not kill the worker
                logger.exception("tool dispatcher poll failed")
            self._wake.wait(timeout=max(0.1, self.settings.tool_worker_poll_seconds))
            self._wake.clear()

    def _claim_and_submit(self) -> None:
        with self._in_flight_lock:
            available = max(
                0,
                self.settings.tool_worker_batch_size - len(self._in_flight),
            )
        if available == 0:
            return
        job_ids = claim_due_jobs(
            self.session_factory,
            worker_id=self.worker_id,
            batch_size=available,
        )
        for job_id in job_ids:
            db = self.session_factory()
            try:
                job = db.get(ToolJob, job_id)
                if not job:
                    continue
                if job.job_type in ALERT_JOB_TYPES:
                    delay = self._alert_limiter.acquire_or_delay()
                    if delay > 0:
                        reschedule_rate_limited_job(
                            self.session_factory,
                            job_id,
                            delay,
                        )
                        continue
                executor = self._executor_for(job.job_type)
                with self._in_flight_lock:
                    self._in_flight.add(job_id)
                future = executor.submit(self._run_job, job_id)
                future.add_done_callback(
                    lambda _future, completed_job_id=job_id: self._job_done(
                        completed_job_id
                    )
                )
            finally:
                db.close()

    def _run_job(self, job_id: int) -> None:
        db = self.session_factory()
        try:
            job = db.get(ToolJob, job_id)
            if not job:
                return
            execute_tool_job(job, self.operations)
            mark_job_success(self.session_factory, job_id)
        except Exception as exc:  # noqa: BLE001
            mark_job_failure(
                self.session_factory,
                job_id,
                str(exc),
                retry_base_seconds=self.settings.tool_worker_retry_base_seconds,
            )
        finally:
            db.close()

    def _job_done(self, job_id: int) -> None:
        with self._in_flight_lock:
            self._in_flight.discard(job_id)
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._started:
            self._poller.join(timeout=max(1.0, self.settings.tool_worker_poll_seconds + 1))
        self._excel_executor.shutdown(wait=True, cancel_futures=False)
        self._case_executor.shutdown(wait=True, cancel_futures=False)
        self._alert_executor.shutdown(wait=True, cancel_futures=False)
