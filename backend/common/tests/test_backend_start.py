"""Tests for the low-memory backend process configuration."""

import pytest

from common.src.env_settings import get_positive_int_env


def test_positive_int_env_uses_default(monkeypatch):
    monkeypatch.delenv("TA_WEB_WORKERS", raising=False)

    assert get_positive_int_env("TA_WEB_WORKERS", 1) == 1


@pytest.mark.parametrize("value", ["0", "-1", "many"])
def test_positive_int_env_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("TA_WEB_WORKERS", value)

    with pytest.raises(ValueError, match="must be a positive integer"):
        get_positive_int_env("TA_WEB_WORKERS", 1)
