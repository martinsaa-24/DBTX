import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbtx.scheduler import ReportScheduler

EXPOSURE = "exposure.jaffle_shop.top_customers"
TEST_A = "test.jaffle_shop.unique_customers_customer_id.c5af1ff4b1"
TEST_B = "test.jaffle_shop.warn_high_frequency_customers"


@pytest.fixture
def report(reports):
    return reports["top_customers"]


def make(report, gate_tests=(TEST_A, TEST_B), render=None):
    rendered = []

    def default_render(r):
        rendered.append(r.name)
        return [Path(f"{r.name}.html")]

    scheduler = ReportScheduler([(report, gate_tests)], render=render or default_render)
    return scheduler, rendered


def outcome(scheduler):
    [o] = scheduler.outcomes.values()
    return o


def test_renders_after_exposure_and_all_gate_tests(report):
    scheduler, rendered = make(report)

    scheduler.node_finished(EXPOSURE, "no-op")
    scheduler.node_finished(TEST_A, "pass")
    assert outcome(scheduler).status == "pending"  # still waiting on TEST_B

    scheduler.node_finished(TEST_B, "warn")  # warnings do not block
    scheduler.finish()

    assert rendered == ["top_customers"]
    assert outcome(scheduler).status == "rendered"
    assert outcome(scheduler).paths == [Path("top_customers.html")]


def test_tests_may_finish_before_the_exposure(report):
    scheduler, rendered = make(report)
    scheduler.node_finished(TEST_A, "pass")
    scheduler.node_finished(TEST_B, "pass")
    assert rendered == []
    scheduler.node_finished(EXPOSURE, "no-op")
    scheduler.finish()
    assert rendered == ["top_customers"]


@pytest.mark.parametrize("status", ["fail", "error", "skipped"])
def test_failing_gate_test_skips_report(report, status):
    scheduler, rendered = make(report)
    scheduler.node_finished(EXPOSURE, "no-op")
    scheduler.node_finished(TEST_A, status)
    scheduler.node_finished(TEST_B, "pass")
    scheduler.finish()

    assert rendered == []
    assert outcome(scheduler).status == "skipped"
    assert f"upstream test {status}: {TEST_A}" in outcome(scheduler).detail


def test_skipped_exposure_skips_report(report):
    scheduler, rendered = make(report)
    scheduler.node_finished(EXPOSURE, "skipped")
    scheduler.finish()
    assert rendered == []
    assert outcome(scheduler).detail == "exposure skipped (upstream failure)"


def test_unreached_exposure_is_skipped_at_finish(report):
    scheduler, rendered = make(report)
    scheduler.node_finished(TEST_A, "pass")
    scheduler.finish()
    assert rendered == []
    assert outcome(scheduler).status == "skipped"
    assert outcome(scheduler).detail == "exposure was not reached in this run"


def test_render_errors_are_captured(report):
    def boom(_):
        raise ValueError("no such column")

    scheduler, _ = make(report, gate_tests=(), render=boom)
    scheduler.node_finished(EXPOSURE, "no-op")
    scheduler.finish()
    assert outcome(scheduler).status == "error"
    assert outcome(scheduler).detail == "ValueError: no such column"


def test_renders_on_the_thread_that_delivered_the_event(report):
    """dbt closes the adapter's connections as soon as `execute_nodes()` returns, so the
    render has to happen on the node thread itself -- while that call is still blocked --
    rather than being left in flight for `finish` to collect."""
    seen = {}

    def record(_):
        seen["thread"] = threading.get_ident()
        return []

    scheduler, _ = make(report, gate_tests=(), render=record)
    scheduler.node_finished(EXPOSURE, "no-op")

    assert seen["thread"] == threading.get_ident()
    assert outcome(scheduler).status == "rendered"  # already done, before finish()


def test_a_rendering_report_does_not_block_dbts_other_threads(report):
    """The render holds a dbt node thread but not the scheduler's lock, so the rest of
    the run keeps reporting its nodes meanwhile."""
    rendering, other_reported = threading.Event(), threading.Event()

    def slow(_):
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


def test_on_event_adapts_dbt_events(report):
    scheduler, rendered = make(report, gate_tests=())

    def event(name, uid=None, status=None):
        node_info = SimpleNamespace(unique_id=uid, node_status=status)
        return SimpleNamespace(info=SimpleNamespace(name=name), data=SimpleNamespace(node_info=node_info))

    scheduler.on_event(event("NodeStart", EXPOSURE, "started"))
    scheduler.on_event(event("NodeFinished", EXPOSURE, "no-op"))
    scheduler.on_event(event("CommandCompleted"))
    assert rendered == ["top_customers"]
    scheduler.finish()  # idempotent
