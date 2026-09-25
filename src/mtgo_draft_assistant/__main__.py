"""
    python -m mtgo_draft_assistant [--log-dir DIR] [--port 8765] [--no-browser]
    python -m mtgo_draft_assistant --arena [--arena-log PATH]

Starts the local tracker and opens it in your browser. Read-only: it only
ever reads files MTGO (or Arena) has already written.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from .arena_log import DEFAULT_LOG as DEFAULT_ARENA_LOG
from .arena_log import ArenaWatcher
from .config import REPO_ROOT, ConfigError, load_config, ratings_settings, resolve_log_dir
from .ratings import (
    DEFAULT_EXPANSION,
    DEFAULT_FORMAT,
    DEFAULT_MIN_GAMES,
    DEFAULT_REFRESH_HOURS,
    RatingsProvider,
)
from .scryfall import CardResolver
from .server import DraftServer

DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "cache"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mtgo_draft_assistant", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log-dir", help="MTGO draft log directory (Settings -> Save Draft Log)")
    ap.add_argument("--arena", action="store_true",
                    help="watch MTG Arena's Player.log instead of MTGO draft logs "
                         "(needs Detailed Logs enabled in Arena's options)")
    ap.add_argument("--arena-log", default=None,
                    help=f"path to Arena's Player.log (default {DEFAULT_ARENA_LOG})")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval", type=float, default=0.5, help="poll interval in seconds")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR),
                    help="where Scryfall card data, images and 17Lands ratings are cached")
    ap.add_argument("--no-cards", action="store_true",
                    help="offline mode: no Scryfall lookups, text-only cards")
    ap.add_argument("--no-ratings", action="store_true",
                    help="skip 17Lands ratings (also: enabled = false under [ratings] in config.toml)")
    ap.add_argument("--ratings-expansion", default=None,
                    help=f"17Lands expansion code (default from config.toml, else {DEFAULT_EXPANSION!r})")
    ap.add_argument("--ratings-format", default=None,
                    help=f"17Lands format (default from config.toml, else {DEFAULT_FORMAT!r})")
    ap.add_argument("--verbose", action="store_true", help="log HTTP requests")
    args = ap.parse_args(argv)

    log_dir = None
    watcher = None
    if args.arena:
        arena_log = Path(args.arena_log).expanduser() if args.arena_log else DEFAULT_ARENA_LOG
        if not arena_log.is_file():
            print(f"warning: {arena_log} not found; waiting for Arena to write it "
                  "(Options -> Account -> Detailed Logs must be on)", file=sys.stderr, flush=True)
        watcher = ArenaWatcher(arena_log, interval=args.interval)
    else:
        try:
            log_dir = resolve_log_dir(args.log_dir)
        except ConfigError:
            print("No draft log directory configured yet. Set it in the web page "
                  "(it's the path from MTGO's Settings -> Save Draft Log).", flush=True)
        if log_dir is not None and not log_dir.is_dir():
            print(f"warning: {log_dir} does not exist yet; waiting for MTGO to create it",
                  file=sys.stderr, flush=True)

    resolver = None if args.no_cards else CardResolver(args.cache_dir)

    rcfg = ratings_settings(load_config())
    ratings = None
    if not args.no_ratings and rcfg.get("enabled", True):
        ratings = RatingsProvider(
            args.cache_dir,
            expansion=args.ratings_expansion or str(rcfg.get("expansion") or DEFAULT_EXPANSION),
            fmt=args.ratings_format or str(rcfg.get("format") or DEFAULT_FORMAT),
            refresh_hours=float(rcfg.get("refresh_hours", DEFAULT_REFRESH_HOURS)),
            min_games=int(rcfg.get("min_games", DEFAULT_MIN_GAMES)),
        )

    server = DraftServer(log_dir, host=args.host, port=args.port,
                         interval=args.interval, verbose=args.verbose,
                         resolver=resolver, ratings=ratings, watcher=watcher)
    server.start()
    if args.arena:
        print(f"MTGO Draft Assistant watching Arena log {watcher.log_path}", flush=True)
    else:
        print(f"MTGO Draft Assistant watching {log_dir or '(no directory set)'}", flush=True)
    if resolver is not None:
        print(f"Card data cached in {args.cache_dir}", flush=True)
    if ratings is not None:
        st = ratings.status()
        have = f"{st['cards']} cards cached from {st['fetched_at']}" if st["cards"] else "no cache yet"
        print(f"17Lands ratings: {ratings.expansion} / {ratings.format} ({have}; "
              f"refreshed every {ratings.refresh_hours:g}h)", flush=True)
    print(f"Open {server.url}  (Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        webbrowser.open(server.url)
    try:
        server.wait()
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
