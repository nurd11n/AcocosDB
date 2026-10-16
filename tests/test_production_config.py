import os
import secrets
import subprocess
import sys

import pytest


def _load_production(key):
    environment = os.environ.copy()
    environment.update(SECRET_KEY=key, REDIS_URL="redis://localhost:6379/0")
    return subprocess.run(
        [sys.executable, "-c", "import config.settings.prod"],
        env=environment,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("key", ["", "change-me", "x" * 64, "django-insecure-" + "abcdef1234" * 8])
def test_production_rejects_missing_placeholder_and_weak_keys(key):
    result = _load_production(key)
    assert result.returncode != 0
    assert "strong random SECRET_KEY" in result.stderr


def test_production_accepts_generated_key_without_printing_it():
    key = secrets.token_urlsafe(64)
    result = _load_production(key)
    assert result.returncode == 0, result.stderr
    assert key not in result.stdout + result.stderr
