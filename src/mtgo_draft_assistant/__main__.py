"""
    python -m mtgo_draft_assistant [--log-dir DIR] [--port 8765] [--no-browser]

Starts the local tracker and opens it in your browser. Read-only: it only
ever reads files MTGO has already written.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser

from .config import ConfigError, resolve_log_dir
from .server import DraftServer


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mtgo_draft_assistant", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log-dir", help="MTGO draft log directory (Settings -> Save Draft Log)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval", type=float, default=0.5, help="poll interval in seconds")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    ap.add_argument("--verbose", action="store_true", help="log HTTP requests")
    args = ap.parse_args(argv)

    try:
        log_dir = resolve_log_dir(args.log_dir)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not log_dir.is_dir():
        print(f"warning: {log_dir} does not exist yet; waiting for MTGO to create it",
              file=sys.stderr)

    server = DraftServer(log_dir, host=args.host, port=args.port,
                         interval=args.interval, verbose=args.verbose)
    server.start()
    print(f"MTGO Draft Assistant watching {log_dir}", flush=True)
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
