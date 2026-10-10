"""The dbtx.conf parser, tested against checked-in sample files.

Behaviour (plan 6.5.3):
* accepts `KEY=VALUE` lines, `#` comment lines, blank lines, surrounding whitespace,
  and values wrapped in matching single or double quotes
* rejects, naming the file and line: unknown keys, duplicate keys, `export`, `${...}`
  interpolation, lines without `=`, unterminated quotes, empty values
* the template shipped to users (examples/dbtx.conf.example) is itself valid
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dbtx import config
from dbtx.config import ConfigError
from tests.unit.conftest import config_sample

EXAMPLE_TEMPLATE = Path(__file__).resolve().parents[2] / "examples" / "dbtx.conf.example"


def test_valid_full_sample():
    assert config.parse_config_file(config_sample("valid_full.conf")) == {
        "DB_BACKEND": ("disabled", 3),
        "DB_URI": ("sqlite:///x.db", 4),
    }


@pytest.mark.parametrize(
    "sample, value",
    [
        ("platform_sqlite.conf", {"DB_URI": ("sqlite:///.dbtx/dbtx.db", 1)}),
        ("platform_postgres.conf", {"DB_URI": ("postgresql+psycopg://dbtx:s3cret@db/dbtx", 1)}),
        ("backend_without_uri.conf", {"DB_BACKEND": ("enabled", 2)}),
        ("backend_disabled.conf", {"DB_BACKEND": ("disabled", 1)}),
        # The parser does not judge values; DB_BACKEND is validated later.
        ("bad_backend_value.conf", {"DB_BACKEND": ("sometimes", 1)}),
    ],
)
def test_valid_samples(sample, value):
    assert config.parse_config_file(config_sample(sample)) == value


def test_shipped_example_template_is_valid():
    entries = config.parse_config_file(EXAMPLE_TEMPLATE)
    assert sorted(entries) == ["DB_BACKEND", "DB_URI"]
    assert entries["DB_BACKEND"][0] == "enabled"
    assert entries["DB_URI"][0] == "sqlite:///.dbtx/dbtx.db"



@pytest.mark.parametrize(
    "sample, error",
    [
        ("invalid_unknown_key.conf", r"invalid_unknown_key.conf:1: unknown key 'DB_URL'"),
        ("invalid_duplicate_key.conf", r"invalid_duplicate_key.conf:2: DB_URI is already set on line 1"),
        ("invalid_export.conf", r"invalid_export.conf:1: `export` is not supported"),
        ("invalid_interpolation.conf", r"invalid_interpolation.conf:1: .* interpolation is not supported"),
        ("invalid_missing_equals.conf", r"invalid_missing_equals.conf:1: expected KEY=VALUE"),
        ("invalid_unterminated_quote.conf", r"invalid_unterminated_quote.conf:1: unterminated quote"),
        ("invalid_empty_value.conf", r"invalid_empty_value.conf:1: DB_URI has an empty value"),
    ],
)
def test_invalid_samples(sample, error):
    with pytest.raises(ConfigError, match=error):
        config.parse_config_file(config_sample(sample))
