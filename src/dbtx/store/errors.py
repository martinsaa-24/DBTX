"""Errors raised by the store."""


class StoreError(Exception):
    """A database problem dbtx reports and stops on.

    At start-up (plan 6.6 step 8) and in `dbtx db` commands it becomes exit code 2,
    before dbt is ever started.
    """
