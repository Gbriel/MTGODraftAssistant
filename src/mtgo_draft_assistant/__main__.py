"""
    python -m mtgo_draft_assistant [--log-dir DIR] [--port 8765] [--no-browser]

Starts the local tracker and opens it in your browser. Watches both the MTGO
draft log folder and Arena's Player.log and shows whichever drafted most
recently (--mtgo or --arena to watch one only). Read-only: it only ever
reads files the clients have already written.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from .arena_cards import open_default as open_arena_db
from .arena_log import DEFAULT_LOG as DEFAULT_ARENA_LOG
from .arena_log import ArenaWatcher
from .auto_watcher import AutoWatcher
from .config import (
    REPO_ROOT,
    ConfigError,
    arena_log_setting,
    cube_settings,
    load_config,
    ratings_settings,
    resolve_log_dir,
)
from .cube_list import load as load_cube
from .ratings import (
    DEFAULT_EXPANSION,
    DEFAULT_FORMAT,
    DEFAULT_MIN_GAMES,
    DEFAULT_REFRESH_HOURS,
    DEFAULT_TIME_PERIOD,
    RatingsClient,
    RatingsPool,
)
from .scryfall import CardResolver
from .server import DraftServer

DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "cache"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mtgo_draft_assistant", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log-dir", help="MTGO draft log directory (Settings -> Save Draft Log)")
    ap.add_argument("--arena", action="store_true",
                    help="watch only MTG Arena's Player.log (by default both MTGO and Arena "
                         "are watched and the one that drafted most recently is shown)")
    ap.add_argument("--mtgo", action="store_true", help="watch only the MTGO draft log folder")
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
    ap.add_argument("--cube", default=None,
                    help="cube card list: a text file (one name per line) or a URL with a "
                         "Color | Card table (default from [cube] list in config.toml)")
    ap.add_argument("--verbose", action="store_true", help="log HTTP requests")
    args = ap.parse_args(argv)

    log_dir = None
    watcher = None
    cfg = load_config()
    configured_arena = arena_log_setting(cfg)        # set from the page; empty = default
    arena_log = (Path(args.arena_log).expanduser() if args.arena_log
                 else Path(configured_arena).expanduser() if configured_arena else DEFAULT_ARENA_LOG)
    if not args.mtgo:
        if not arena_log.is_file():
            print(f"Arena log not found at {arena_log}; will use it if it appears "
                  "(Options -> Account -> Detailed Logs must be on)", flush=True)
    if not args.arena:
        try:
            log_dir = resolve_log_dir(args.log_dir)
        except ConfigError:
            print("No MTGO draft log directory configured yet. Set it in the web page "
                  "(it's the path from MTGO's Settings -> Save Draft Log).", flush=True)
        if log_dir is not None and not log_dir.is_dir():
            print(f"warning: {log_dir} does not exist yet; waiting for MTGO to create it",
                  file=sys.stderr, flush=True)
    if args.arena:
        watcher = ArenaWatcher(arena_log, interval=args.interval)
    elif not args.mtgo:
        watcher = AutoWatcher(log_dir, arena_log, interval=args.interval)

    resolver = None if args.no_cards else CardResolver(args.cache_dir)

    rcfg = ratings_settings(cfg)
    pool = None
    if not args.no_ratings and rcfg.get("enabled", True):
        expansion = args.ratings_expansion or str(rcfg.get("expansion") or DEFAULT_EXPANSION)
        fmt = args.ratings_format or str(rcfg.get("format") or DEFAULT_FORMAT)
        pool = RatingsPool(
            args.cache_dir, client=RatingsClient(),          # shared: pulls go out one at a time
            refresh_hours=float(rcfg.get("refresh_hours", DEFAULT_REFRESH_HOURS)),
            min_games=int(rcfg.get("min_games", DEFAULT_MIN_GAMES)),
            default=(expansion, fmt),
            time_period=str(rcfg.get("time_period") or DEFAULT_TIME_PERIOD),
        )
        # the default dataset (MTGO drafts, and Arena before a draft is seen) plus the
        # page's toggles; an Arena set draft adds its own set's datasets on demand
        pool.sets_for(expansion, fmt)

    cube = None
    cube_spec = args.cube or cube_settings(cfg).get("list")
    if cube_spec:
        try:
            cube = load_cube(str(cube_spec), args.cache_dir)
        except Exception as e:  # a missing file or a dead URL should not stop the tracker
            print(f"warning: cube list {cube_spec!r} not loaded: {e}", file=sys.stderr, flush=True)

    arena_db = None if args.mtgo else open_arena_db()
    server = DraftServer(log_dir, host=args.host, port=args.port,
                         interval=args.interval, verbose=args.verbose,
                         resolver=resolver, ratings_pool=pool,
                         watcher=watcher, cube=cube, arena_db=arena_db)
    server.start()
    if not args.mtgo:
        print(f"Arena card database: {arena_db.path if arena_db else 'not found (names fall back to 17Lands and Scryfall)'}",
              flush=True)
    if cube is not None:
        print(f"Cube list: {cube.name} ({len(cube)} cards, {cube.source})", flush=True)
    elif not args.mtgo and pool is not None:
        print("Cube list: 17Lands card list for Arena drafts; none for MTGO (set [cube] list)", flush=True)
    if args.arena:
        print(f"Watching Arena log {watcher.log_path}", flush=True)
    elif args.mtgo:
        print(f"Watching MTGO folder {log_dir or '(no directory set)'}", flush=True)
    else:
        print(f"Watching MTGO folder {log_dir or '(no directory set)'} and Arena log {arena_log}; "
              "showing whichever drafted most recently", flush=True)
    if resolver is not None:
        print(f"Card data cached in {args.cache_dir}", flush=True)
    if pool is not None:
        primary = pool.get(*pool.default, create=False)
        st = primary.status() if primary else {"cards": 0}
        have = f"{st['cards']} cards cached from {st['fetched_at']}" if st["cards"] else "no cache yet"
        print(f"17Lands ratings: default {pool.default[0]} / {pool.default[1]} / {pool.time_period} "
              f"({have}; refreshed every {pool.refresh_hours:g}h). An Arena set draft loads its "
              f"own set's data automatically.", flush=True)
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
