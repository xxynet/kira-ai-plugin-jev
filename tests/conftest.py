"""Locate KiraAI and isolate runtime files when testing the plugin checkout."""

import logging
import os
import sys
import tempfile
from pathlib import Path

import pytest


def _framework_root() -> Path:
    configured = os.environ.get("KIRAAI_ROOT")
    candidates = [Path(configured).expanduser().resolve()] if configured else Path(__file__).resolve().parents
    for candidate in candidates:
        if (candidate / "core/plugin/plugin_context.py").is_file():
            return candidate
    raise pytest.UsageError(
        "KiraAI source checkout not found. Set KIRAAI_ROOT to a KiraAI 3.x repository "
        "with its requirements installed."
    )


sys.path.insert(0, str(_framework_root()))

from core.utils.path_utils import init_paths

_runtime = tempfile.TemporaryDirectory(prefix="jev-tests-")
init_paths(data_dir=_runtime.name)


def pytest_unconfigure(config):
    """Release framework log files before removing the temporary runtime."""
    logging.shutdown()
    _runtime.cleanup()
