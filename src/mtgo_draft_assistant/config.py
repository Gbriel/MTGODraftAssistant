"""
Where is the draft log directory?

MTGO stores it in a settings file it doesn't flush promptly (DESIGN.md §2.1),
so we don't read that. Resolution order:

  1. ``--log-dir`` on the command line
  2. ``MTGO_DRAFT_LOG_DIR`` environment variable
  3. ``log_dir`` in ``config.toml`` (current directory, then the repo root)

``config.toml`` may also hold a ``[ratings]`` table for 17Lands (see
``config.example.toml``).
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

ENV_VAR = "MTGO_DRAFT_LOG_DIR"
CONFIG_NAME = "config.toml"
REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(Exception):
    pass


def config_candidates() -> list[Path]:
    return [Path.cwd() / CONFIG_NAME, REPO_ROOT / CONFIG_NAME]


def load_config() -> dict:
    for path in config_candidates():
        if path.is_file():
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
            data["_source"] = str(path)
            return data
    return {}


_LOG_DIR_LINE = re.compile(r"^\s*log_dir\s*=.*$", re.MULTILINE)


def save_log_dir(log_dir: str | Path, path: Path | None = None) -> Path:
    """
    Persist the directory to config.toml (repo root by default) so the next
    start picks it up. Written with forward slashes, which TOML and Windows
    both accept, so no escaping games. Anything else in the file (a
    ``[ratings]`` table, comments) is kept as is.
    """
    target = path or (REPO_ROOT / CONFIG_NAME)
    value = str(Path(log_dir)).replace("\\", "/")
    line = f"log_dir = {json.dumps(value, ensure_ascii=False)}"
    try:
        existing = target.read_text(encoding="utf-8")
    except OSError:
        existing = None
    if existing is None:
        body = (
            "# MTGO draft log directory: the path from MTGO's Settings -> Save Draft Log.\n"
            "# Written by the web UI; edit by hand if you prefer.\n"
            f"{line}\n"
        )
    elif _LOG_DIR_LINE.search(existing):
        body = _LOG_DIR_LINE.sub(line, existing, count=1)
    else:
        # top-level keys must precede any [table]; put it first
        body = f"{line}\n{existing}"
    target.write_text(body, encoding="utf-8")
    return target


def cube_settings(cfg: dict | None = None) -> dict:
    """The ``[cube]`` table: ``list`` is a path or URL of the cube card list."""
    if cfg is None:
        cfg = load_config()
    table = cfg.get("cube")
    return dict(table) if isinstance(table, dict) else {}


def ratings_settings(cfg: dict | None = None) -> dict:
    """
    The ``[ratings]`` table from config.toml, or ``{}``. Keys the CLI
    understands: ``enabled`` (bool), ``expansion``, ``format``,
    ``refresh_hours``, ``min_games``.
    """
    if cfg is None:
        cfg = load_config()
    table = cfg.get("ratings")
    return dict(table) if isinstance(table, dict) else {}


def resolve_log_dir(cli_value: str | None = None) -> Path:
    if cli_value:
        return Path(cli_value).expanduser()
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env).expanduser()
    cfg = load_config()
    if cfg.get("log_dir"):
        return Path(str(cfg["log_dir"])).expanduser()
    looked = ", ".join(str(p) for p in config_candidates())
    raise ConfigError(
        "No draft log directory configured. Pass --log-dir, set "
        f"{ENV_VAR}, or create {CONFIG_NAME} with a log_dir entry (looked in: {looked}). "
        "The path is whatever you typed into MTGO's Settings -> Save Draft Log."
    )
