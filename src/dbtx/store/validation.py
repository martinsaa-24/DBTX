"""App-enforced values (plan D9): values that come from dbt, checked in Python.

A dbt upgrade can add a value (a new node status), and that must not break a run. So
in production an unknown value is stored, with a warning logged once per value. In
strict mode (`DBTX_DB_STRICT=1`, set for the whole test suite) it raises instead, so
CI sees new values. `DBTX_DB_STRICT` is a test switch, read straight from the
environment, never a configuration option (plan 6.4).
"""

from __future__ import annotations

import logging
import os

from dbtx.store.errors import StoreError

STRICT_ENV_VAR = "DBTX_DB_STRICT"

logger = logging.getLogger("dbtx.store")

# (column, value) pairs already warned about in this process.
_warned: set[tuple[str, str]] = set()


class UnknownValueError(StoreError):
    """An app-enforced column got a value outside its known set, in strict mode."""


def check_known(column: str, value: str, known: frozenset[str]) -> None:
    """Pass a known value. An unknown one raises in strict mode, else warns once."""
    if value in known:
        return
    known_text = ", ".join(sorted(known))
    if os.environ.get(STRICT_ENV_VAR) == "1":
        raise UnknownValueError(
            f"{column}: unknown value {value!r} (known: {known_text}); strict mode "
            f"({STRICT_ENV_VAR}=1) refuses it"
        )
    if (column, value) in _warned:
        return
    _warned.add((column, value))
    logger.warning(
        f"{column}: storing unknown value {value!r} (known: {known_text}); a newer dbt "
        f"may have added it"
    )
