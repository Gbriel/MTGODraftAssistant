"""
Where is the draft log directory?

MTGO stores it in a settings file it doesn't flush promptly (DESIGN.md §2.1),
so we don't read that. Resolution order:

  1. ``--log-dir`` on the command line
  2. ``MTGO_DRAFT_LOG_DIR`` environment variable
  3. ``log_dir`` in ``config.toml`` (current directory, then the repo root)
"""

from __future__ import annotations

import json
import os
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


def save_log_dir(log_dir: str | Path, path: Path | None = None) -> Path:
    """
    Persist the directory to config.toml (repo root by default) so the next
    start picks it up. Written with forward slashes, which TOML and Windows
    both accept, so no escaping games.
    """
    target = path or (REPO_ROOT / CONFIG_NAME)
    value = str(Path(log_dir)).replace("\\", "/")
    body = (
        "# MTGO draft log directory: the path from MTGO's Settings -> Save Draft Log.\n"
        "# Written by the web UI; edit by hand if you prefer.\n"
        f"log_dir = {json.dumps(value, ensure_ascii=False)}\n"
    )
    target.write_text(body, encoding="utf-8")
    return target


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
