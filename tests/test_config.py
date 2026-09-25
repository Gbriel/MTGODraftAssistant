"""config.toml round trip."""

from __future__ import annotations

import os
import sys
import tomllib

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mtgo_draft_assistant.config import (  # noqa: E402
    ConfigError,
    load_config,
    ratings_settings,
    resolve_log_dir,
    save_log_dir,
)


@pytest.mark.parametrize("raw", [
    r"C:\Users\someone\MTGODraftLogs",
    "C:/Users/someone/MTGO Draft Logs",
    r"D:\Games\Magic Online\logs with 'quotes'",
    "/home/someone/mtgo",
    "C:\\Users\\kopít\\Palantír",
])
def test_save_and_load_round_trip(tmp_path, raw):
    cfg = tmp_path / "config.toml"
    save_log_dir(raw, cfg)
    with open(cfg, "rb") as fh:
        loaded = tomllib.load(fh)["log_dir"]
    assert os.path.normpath(loaded) == os.path.normpath(raw)


def test_cli_beats_env_beats_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MTGO_DRAFT_LOG_DIR", str(tmp_path / "env"))
    assert resolve_log_dir(str(tmp_path / "cli")) == tmp_path / "cli"
    assert resolve_log_dir(None) == tmp_path / "env"
    monkeypatch.delenv("MTGO_DRAFT_LOG_DIR")
    monkeypatch.chdir(tmp_path)
    save_log_dir(tmp_path / "cfg", tmp_path / "config.toml")
    assert resolve_log_dir(None) == tmp_path / "cfg"


def test_save_log_dir_keeps_ratings_table(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '# my notes\nlog_dir = "C:/old"\n\n[ratings]\nexpansion = "Cube"\nmin_games = 300\n',
        encoding="utf-8",
    )
    save_log_dir("C:/new", cfg)
    with open(cfg, "rb") as fh:
        loaded = tomllib.load(fh)
    assert loaded["log_dir"] == "C:/new"
    assert loaded["ratings"] == {"expansion": "Cube", "min_games": 300}
    assert "# my notes" in cfg.read_text(encoding="utf-8")

    # a file with only a [ratings] table gets log_dir inserted before it
    cfg.write_text('[ratings]\nenabled = false\n', encoding="utf-8")
    save_log_dir("C:/x", cfg)
    with open(cfg, "rb") as fh:
        loaded = tomllib.load(fh)
    assert loaded["log_dir"] == "C:/x" and loaded["ratings"] == {"enabled": False}

    monkeypatch.chdir(tmp_path)
    assert ratings_settings(load_config()) == {"enabled": False}
    assert ratings_settings({}) == {}
    assert ratings_settings({"ratings": "nope"}) == {}


def test_missing_everything_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("MTGO_DRAFT_LOG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mtgo_draft_assistant.config.REPO_ROOT", tmp_path)
    with pytest.raises(ConfigError):
        resolve_log_dir(None)
