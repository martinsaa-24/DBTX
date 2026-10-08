"""Decides when each report may render, based on dbt's node events.

A report becomes ready when, in the current `dbt build`:
  * its exposure node finished (dbt only runs it once every upstream node succeeded), and
  * every data test in this run on the exposure's direct parents passed (or warned).

dbt runs exposures concurrently with their parents' tests, which is why the second
condition is tracked here instead of relying on dbt's skip propagation.

A ready report renders on the dbt thread that delivered the event making it ready, which
is what keeps its query inside the adapter's lifetime. dbt closes every connection in
`execute_with_hooks`, well before the end of the command, and that cannot run until
`execute_nodes()` returns -- which cannot happen while one of its node threads is still
inside this callback. Rendering anywhere else races that teardown.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
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
    ) -> None:
        self._plans = {r.exposure_id: _Plan(r, frozenset(tests)) for r, tests in reports}
        self._render = render
        self._statuses: dict[str, str] = {}
        self._lock = threading.Lock()
        self._finished = False

    @property
    def outcomes(self) -> dict[str, Outcome]:
        return {p.report.name: p.outcome for p in self._plans.values()}

    def on_event(self, event) -> None:
        """dbtRunner callback, run on whichever of dbt's threads fired the event."""
        name = event.info.name
        if name == "NodeFinished":
            info = event.data.node_info
            self.node_finished(info.unique_id, info.node_status)
        elif name == "CommandCompleted":
            self.finish()

    def node_finished(self, unique_id: str, status: str) -> None:
        ready: list[_Plan] = []
        with self._lock:
            self._statuses[unique_id] = status
            for plan in self._plans.values():
                if plan.outcome.status == "pending" and (
                    unique_id == plan.report.exposure_id or unique_id in plan.gate_tests
                ):
                    if self._evaluate(plan):
                        ready.append(plan)
        # On this thread, so dbt cannot close the adapter's connections underneath the
        # query, but outside the lock, so dbt's other node threads keep reporting theirs.
        for plan in ready:
            self._run(plan)

    def finish(self) -> None:
        """Mark every report the run never reached.

        Nothing can still be rendering here: renders happen on dbt's node threads, and
        dbt fires `CommandCompleted` only once all of them have returned.
        """
        with self._lock:
            if self._finished:
                return
            self._finished = True
            for plan in self._plans.values():
                if plan.outcome.status == "pending":
                    plan.outcome = Outcome("skipped", "exposure was not reached in this run")

    def _evaluate(self, plan: _Plan) -> bool:
        """Settle `plan`'s outcome where it can be, returning True when it is now ready.

        Called with the lock held; the caller renders what this hands back.
        """
        exposure_status = self._statuses.get(plan.report.exposure_id)
        if exposure_status is None:
            return False
        if exposure_status not in EXPOSURE_OK:
            plan.outcome = Outcome("skipped", f"exposure {exposure_status} (upstream failure)")
            return False

        for test in sorted(plan.gate_tests):
            status = self._statuses.get(test)
            if status is None:
                return False  # still waiting on this test
            if status not in TEST_OK:
                plan.outcome = Outcome("skipped", f"upstream test {status}: {test}")
                return False

        plan.outcome = Outcome("running")
        return True

    def _run(self, plan: _Plan) -> None:
        try:
            plan.outcome = Outcome("rendered", paths=self._render(plan.report))
        except Exception as exc:
            plan.outcome = Outcome("error", f"{type(exc).__name__}: {exc}")
