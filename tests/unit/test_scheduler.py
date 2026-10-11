import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbtx.reports import Report
from dbtx.scheduler import Outcome, ReportScheduler

EXPOSURE = "exposure.jaffle_shop.top_customers"
TEST_A = "test.jaffle_shop.unique_customers_customer_id.c5af1ff4b1"
TEST_B = "test.jaffle_shop.warn_high_frequency_customers"


@pytest.fixture
def report(reports: dict[str, Report]) -> Report:
    return reports["top_customers"]


def make(
    report: Report,
    gate_tests: tuple[str, ...] = (TEST_A, TEST_B),
    render: Callable[[Report], list[Path]] | None = None,
) -> tuple[ReportScheduler, list[str]]:
    rendered = []

    def default_render(r: Report) -> list[Path]:
        rendered.append(r.name)
        return [Path(f"{r.name}.html")]

    scheduler = ReportScheduler([(report, gate_tests)], render=render or default_render)
    return scheduler, rendered


def outcome(scheduler: ReportScheduler) -> Outcome:
    [o] = scheduler.outcomes.values()
    return o


def test_renders_after_exposure_and_all_gate_tests(report: Report) -> None:
    scheduler, rendered = make(report)

    scheduler.node_finished(EXPOSURE, "no-op")
    scheduler.node_finished(TEST_A, "pass")
    assert outcome(scheduler).status == "pending"  # still waiting on TEST_B

    scheduler.node_finished(TEST_B, "warn")  # warnings do not block
    scheduler.finish()

    assert rendered == ["top_customers"]
    assert outcome(scheduler).status == "rendered"
    assert outcome(scheduler).paths == [Path("top_customers.html")]


def test_tests_may_finish_before_the_exposure(report: Report) -> None:
    scheduler, rendered = make(report)
    scheduler.node_finished(TEST_A, "pass")
    scheduler.node_finished(TEST_B, "pass")
    assert rendered == []
    scheduler.node_finished(EXPOSURE, "no-op")
    scheduler.finish()
    assert rendered == ["top_customers"]


@pytest.mark.parametrize("status", ["fail", "error", "skipped"])
def test_failing_gate_test_skips_report(report: Report, status: str) -> None:
    scheduler, rendered = make(report)
    scheduler.node_finished(EXPOSURE, "no-op")
    scheduler.node_finished(TEST_A, status)
    scheduler.node_finished(TEST_B, "pass")
    scheduler.finish()

    assert rendered == []
    assert outcome(scheduler).status == "skipped"
    assert f"upstream test {status}: {TEST_A}" in outcome(scheduler).detail


def test_skipped_exposure_skips_report(report: Report) -> None:
    scheduler, rendered = make(report)
    scheduler.node_finished(EXPOSURE, "skipped")
    scheduler.finish()
    assert rendered == []
    assert outcome(scheduler).detail == "exposure skipped (upstream failure)"


def test_unreached_exposure_is_skipped_at_finish(report: Report) -> None:
    scheduler, rendered = make(report)
    scheduler.node_finished(TEST_A, "pass")
    scheduler.finish()
    assert rendered == []
    assert outcome(scheduler).status == "skipped"
    assert outcome(scheduler).detail == "exposure was not reached in this run"


def test_render_errors_are_captured(report: Report) -> None:
    def boom(_: Report) -> list[Path]:
        raise ValueError("no such column")

    scheduler, _ = make(report, gate_tests=(), render=boom)
    scheduler.node_finished(EXPOSURE, "no-op")
    scheduler.finish()
    assert outcome(scheduler).status == "error"
    assert outcome(scheduler).detail == "ValueError: no such column"


def test_renders_on_the_thread_that_delivered_the_event(report: Report) -> None:
    """dbt closes the adapter's connections as soon as `execute_nodes()` returns, so the
    render has to happen on the node thread itself -- while that call is still blocked --
    rather than being left in flight for `finish` to collect."""
    seen = {}

    def record(_: Report) -> list[Path]:
        seen["thread"] = threading.get_ident()
        return []

    scheduler, _ = make(report, gate_tests=(), render=record)
    scheduler.node_finished(EXPOSURE, "no-op")

    assert seen["thread"] == threading.get_ident()
    assert outcome(scheduler).status == "rendered"  # already done, before finish()


def test_a_rendering_report_does_not_block_dbts_other_threads(report: Report) -> None:
    """The render holds a dbt node thread but not the scheduler's lock, so the rest of
    the run keeps reporting its nodes meanwhile."""
    rendering, other_reported = threading.Event(), threading.Event()

    def slow(_: Report) -> list[Path]:
        rendering.set()
        assert other_reported.wait(5), "another dbt thread was blocked by the render"
        return []

    scheduler, _ = make(report, gate_tests=(), render=slow)
    node_thread = threading.Thread(target=scheduler.node_finished, args=(EXPOSURE, "no-op"))
    node_thread.start()
    assert rendering.wait(5)

    scheduler.node_finished(TEST_A, "pass")  # another thread's event still gets through
    other_reported.set()

    node_thread.join(5)
    assert outcome(scheduler).status == "rendered"


def test_on_event_adapts_dbt_events(report: Report) -> None:
    scheduler, rendered = make(report, gate_tests=())

    def event(name: str, uid: str | None = None, status: str | None = None) -> SimpleNamespace:
        node_info = SimpleNamespace(unique_id=uid, node_status=status)
        return SimpleNamespace(info=SimpleNamespace(name=name), data=SimpleNamespace(node_info=node_info))

    scheduler.on_event(event("NodeStart", EXPOSURE, "started"))
    scheduler.on_event(event("NodeFinished", EXPOSURE, "no-op"))
    scheduler.on_event(event("CommandCompleted"))
    assert rendered == ["top_customers"]
    scheduler.finish()  # idempotent


# --------------------------------------------------------------------------------------
# The observer, `on_change(exposure_id, outcome)` (plan 5.4): called after every outcome
# transition, outside the scheduler's lock, so the store can record each report's
# lifecycle without the scheduler knowing about it.
# --------------------------------------------------------------------------------------


class Observer:
    """Records every (exposure_id, status, detail) it is told about, and whether the
    scheduler's lock was free at the time."""

    def __init__(self) -> None:
        self.scheduler: ReportScheduler | None = None
        self.changes: list[tuple[str, str, str]] = []
        self.lock_was_free: list[bool] = []

    def __call__(self, exposure_id: str, changed: Outcome) -> None:
        assert self.scheduler is not None
        self.changes.append((exposure_id, changed.status, changed.detail))
        free = self.scheduler._lock.acquire(blocking=False)
        if free:
            self.scheduler._lock.release()
        self.lock_was_free.append(free)


def observed(
    report: Report,
    gate_tests: tuple[str, ...] = (TEST_A, TEST_B),
    render: Callable[[Report], list[Path]] | None = None,
) -> tuple[ReportScheduler, Observer]:
    observer = Observer()

    def default_render(r: Report) -> list[Path]:
        return [Path(f"{r.name}.html")]

    scheduler = ReportScheduler(
        [(report, gate_tests)], render=render or default_render, on_change=observer
    )
    observer.scheduler = scheduler
    return scheduler, observer


def test_observer_sees_running_then_rendered(report: Report) -> None:
    scheduler, observer = observed(report)
    scheduler.node_finished(EXPOSURE, "success")
    scheduler.node_finished(TEST_A, "pass")
    assert observer.changes == []  # still pending: nothing changed yet
    scheduler.node_finished(TEST_B, "pass")
    scheduler.finish()
    assert observer.changes == [(EXPOSURE, "running", ""), (EXPOSURE, "rendered", "")]


def test_observer_gets_the_rendered_paths(report: Report) -> None:
    seen: list[Outcome] = []
    scheduler = ReportScheduler(
        [(report, ())],
        render=lambda r: [Path("a.html"), Path("a.csv")],
        on_change=lambda exposure_id, changed: seen.append(changed),
    )
    scheduler.node_finished(EXPOSURE, "success")
    assert [o.status for o in seen] == ["running", "rendered"]
    assert seen[-1].paths == [Path("a.html"), Path("a.csv")]


def test_observer_sees_running_then_error(report: Report) -> None:
    def broken(_: Report) -> list[Path]:
        raise RuntimeError("no such table")

    scheduler, observer = observed(report, gate_tests=(), render=broken)
    scheduler.node_finished(EXPOSURE, "success")
    assert observer.changes == [
        (EXPOSURE, "running", ""),
        (EXPOSURE, "error", "RuntimeError: no such table"),
    ]


def test_observer_sees_a_skip_from_a_failed_gate_test(report: Report) -> None:
    scheduler, observer = observed(report)
    scheduler.node_finished(EXPOSURE, "success")
    scheduler.node_finished(TEST_A, "fail")
    assert observer.changes == [(EXPOSURE, "skipped", f"upstream test fail: {TEST_A}")]


def test_observer_sees_a_skip_from_a_failed_exposure(report: Report) -> None:
    scheduler, observer = observed(report)
    scheduler.node_finished(EXPOSURE, "skipped")
    assert observer.changes == [(EXPOSURE, "skipped", "exposure skipped (upstream failure)")]


def test_observer_sees_a_skip_at_finish(report: Report) -> None:
    scheduler, observer = observed(report)
    scheduler.finish()
    scheduler.finish()  # the second finish changes nothing
    assert observer.changes == [(EXPOSURE, "skipped", "exposure was not reached in this run")]


def test_observer_is_not_told_about_settled_reports(report: Report) -> None:
    scheduler, observer = observed(report, gate_tests=())
    scheduler.node_finished(EXPOSURE, "success")
    scheduler.node_finished(EXPOSURE, "success")  # dbt never does this; must not re-render
    scheduler.node_finished(TEST_A, "pass")  # not a gate test any more: nothing changes
    scheduler.finish()
    assert observer.changes == [(EXPOSURE, "running", ""), (EXPOSURE, "rendered", "")]


def test_observer_is_called_outside_the_lock(report: Report) -> None:
    # Every kind of transition: running, rendered, skipped (by an event and at finish).
    scheduler, observer = observed(report, gate_tests=())
    scheduler.node_finished(EXPOSURE, "success")
    other, other_observer = observed(report)
    other.node_finished(EXPOSURE, "error")
    last, last_observer = observed(report)
    last.finish()
    assert observer.lock_was_free == [True, True]
    assert other_observer.lock_was_free == [True]
    assert last_observer.lock_was_free == [True]


def test_observer_is_called_on_the_thread_that_delivered_the_event(report: Report) -> None:
    threads: list[int] = []
    scheduler = ReportScheduler(
        [(report, ())],
        render=lambda r: [],
        on_change=lambda exposure_id, changed: threads.append(threading.get_ident()),
    )
    node_thread = threading.Thread(target=scheduler.node_finished, args=(EXPOSURE, "success"))
    node_thread.start()
    node_thread.join(5)
    assert threads == [node_thread.ident, node_thread.ident]


def test_without_an_observer_nothing_changes(report: Report) -> None:
    scheduler, rendered = make(report, gate_tests=())
    scheduler.node_finished(EXPOSURE, "success")
    scheduler.finish()
    assert rendered == ["top_customers"]
    assert outcome(scheduler).status == "rendered"
