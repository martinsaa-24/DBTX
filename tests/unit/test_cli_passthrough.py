"""Commands dbtx hands to dbt, and how dbt's outcome becomes dbtx's exit code.

Behaviour:
* a command line dbt refuses (no such command, no such option, a bad argument) exits
  2 **with dbt's reason on stderr**, in both modes. dbt's in-process runner returns
  these errors instead of printing them (they happen before dbt's logging starts), so
  dbtx prints them: `dbtx: <reason> Try 'dbtx --help' for help.`
* `exit_code(result)` mirrors dbt: 0 success, 1 a failure dbt reported (failed tests),
  2 an error. Only a usage error is printed: every other error dbt has already logged
  through its own events, so printing it would show it twice
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dbt.cli.exceptions import DbtUsageException
from dbt.cli.main import dbtRunnerResult
from dbt_common.exceptions import DbtRuntimeError

from dbtx import cli
from dbtx.runner import exit_code

# --------------------------------------------------------------------------------------
# Through `cli.main`, with the real dbt (it refuses these before loading any project)
# --------------------------------------------------------------------------------------


def test_an_unknown_command_is_reported_in_platform_mode(
    platform: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["load", "--project-dir", "jaffle-shop"]) == 2
    assert capsys.readouterr().err.endswith(
        "dbtx: No such command 'load'. Try 'dbtx --help' for help.\n"
    )


def test_an_unknown_command_is_reported_in_standalone_mode(
    lone_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(lone_project)
    assert cli.main(["nosuchcommand", "--standalone-mode"]) == 2
    assert capsys.readouterr().err == (
        "dbtx: No such command 'nosuchcommand'. Try 'dbtx --help' for help.\n"
    )


def test_an_unknown_option_to_a_dbt_command_is_reported(
    platform: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["ls", "--no-such-option", "--project-dir", "jaffle-shop"]) == 2
    assert capsys.readouterr().err.endswith(
        "dbtx: No such option '--no-such-option'. Try 'dbtx --help' for help.\n"
    )


# --------------------------------------------------------------------------------------
# exit_code
# --------------------------------------------------------------------------------------


def test_success_is_0_and_silent(capsys: pytest.CaptureFixture[str]) -> None:
    assert exit_code(dbtRunnerResult(success=True)) == 0
    assert capsys.readouterr().err == ""


def test_a_failure_dbt_reported_is_1_and_silent(capsys: pytest.CaptureFixture[str]) -> None:
    # Failed tests: dbt printed them; there is no exception.
    assert exit_code(dbtRunnerResult(success=False)) == 1
    assert capsys.readouterr().err == ""


def test_an_error_dbt_logged_is_2_and_not_printed_again(capsys: pytest.CaptureFixture[str]) -> None:
    result = dbtRunnerResult(success=False, exception=DbtRuntimeError("Could not find profile named 'x'"))
    assert exit_code(result) == 2
    assert capsys.readouterr().err == ""


def test_a_usage_error_is_2_and_printed_once(capsys: pytest.CaptureFixture[str]) -> None:
    result = dbtRunnerResult(success=False, exception=DbtUsageException("No such command 'load'."))
    assert exit_code(result) == 2
    assert capsys.readouterr().err == "dbtx: No such command 'load'. Try 'dbtx --help' for help.\n"
