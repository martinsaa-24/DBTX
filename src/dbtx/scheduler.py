"""Decides when each report may render, based on dbt's node events.

A report becomes ready when, in the current `dbt build`:
  * its exposure node finished (dbt only runs it once every upstream node succeeded), and
  * every data test in this run on the exposure's direct parents passed (or warned).

dbt runs exposures concurrently with their parents' tests, which is why the second
condition is tracked here instead of relying on dbt's skip propagation.
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from dbtx.reports import Report

EXPOSURE_OK = {"success", "no-op"}
TEST_OK = {"pass", "warn", "success"}


@dataclass
class Outcome:
    status: str  # pending | running | rendered | skipped | error
    detail: str = ""
    paths: list[Path] = field(default_factory=list)


@dataclass
class _Plan:
    report: Report
    gate_tests: frozenset[str]
    outcome: Outcome = field(default_factory=lambda: Outcome("pending"))


class ReportScheduler:
    def __init__(
        self,
        reports: Iterable[tuple[Report, Iterable[str]]],
        render: Callable[[Report], list[Path]],
        max_workers: int = 4,
    ) -> None:
        self._plans = {r.exposure_id: _Plan(r, frozenset(tests)) for r, tests in reports}
        self._render = render
        self._statuses: dict[str, str] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dbtx-report")
        self._futures: list[Future] = []
        self._finished = False

    @property
    def outcomes(self) -> dict[str, Outcome]:
        return {p.report.name: p.outcome for p in self._plans.values()}

    def on_event(self, event) -> None:
        """dbtRunner callback. Must stay cheap: it runs on dbt's own threads."""
        name = event.info.name
        if name == "NodeFinished":
            info = event.data.node_info
            self.node_finished(info.unique_id, info.node_status)
        elif name == "CommandCompleted":
            # Fired before dbt closes adapter connections, so reports still in flight
            # can finish their queries.
            self.finish()

    def node_finished(self, unique_id: str, status: str) -> None:
        with self._lock:
            self._statuses[unique_id] = status
            for plan in self._plans.values():
                if plan.outcome.status == "pending" and (
                    unique_id == plan.report.exposure_id or unique_id in plan.gate_tests
                ):
                    self._evaluate(plan)

    def finish(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        for future in list(self._futures):
            future.result()
        self._pool.shutdown()
        for plan in self._plans.values():
            if plan.outcome.status == "pending":
                plan.outcome = Outcome("skipped", "exposure was not reached in this run")

    def _evaluate(self, plan: _Plan) -> None:
        exposure_status = self._statuses.get(plan.report.exposure_id)
        if exposure_status is None:
            return
        if exposure_status not in EXPOSURE_OK:
            plan.outcome = Outcome("skipped", f"exposure {exposure_status} (upstream failure)")
            return

        for test in sorted(plan.gate_tests):
            status = self._statuses.get(test)
            if status is None:
                return  # still waiting on this test
            if status not in TEST_OK:
                plan.outcome = Outcome("skipped", f"upstream test {status}: {test}")
                return

        plan.outcome = Outcome("running")
        # Carry dbt's invocation context (contextvars) into the worker thread,
        # as dbt does for its own thread pool.
        ctx = contextvars.copy_context()
        self._futures.append(self._pool.submit(ctx.run, self._run, plan))

    def _run(self, plan: _Plan) -> None:
        try:
            plan.outcome = Outcome("rendered", paths=self._render(plan.report))
        except Exception as exc:
            plan.outcome = Outcome("error", f"{type(exc).__name__}: {exc}")
