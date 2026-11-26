"""Standalone entrypoints (scripts, worker) must work without importing the FastAPI app.

The test suite always imports `app.main`, which registers every model. A script that
imports only some models would pass the tests yet crash in production, so these checks
run each entrypoint in a fresh interpreter.
"""

import os
import subprocess
import sys

import pytest

ENTRYPOINTS = [
    "app.scripts.create_admin",
    "app.scripts.rotate_field_encryption",
    "app.workers.settings",
]


@pytest.mark.parametrize("module", ENTRYPOINTS)
def test_entrypoint_configures_all_mappers(module: str) -> None:
    code = f"import {module}\nfrom sqlalchemy.orm import configure_mappers\nconfigure_mappers()\n"
    result = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "ENVIRONMENT": "test"},
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
