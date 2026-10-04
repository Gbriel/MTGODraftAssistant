"""Test-wide guards."""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


@pytest.fixture(autouse=True)
def _never_touch_the_real_config(tmp_path, monkeypatch):
    """
    A server created without ``config_path`` saves settings to the repo's
    config.toml. Point that default at a temp directory for every test so
    no test can overwrite the user's real configuration.
    """
    from mtgo_draft_assistant import config
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "repo")
    (tmp_path / "repo").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)
