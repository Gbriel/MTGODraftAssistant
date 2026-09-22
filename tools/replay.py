"""
Replay the fixture snapshots into a scratch directory as if MTGO were writing
them, so you can watch the UI update without being in a draft.

    python tools/replay.py --dest C:\\path\\to\\empty\\dir --delay 3
    python -m mtgo_draft_assistant --log-dir C:\\path\\to\\empty\\dir

Never point --dest at the real MTGO log directory. As a guard, the destination
must be empty or contain only a previous replay's file.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_DIR = os.path.join(ROOT, "tests", "fixtures", "snapshots")
FINAL = os.path.join(ROOT, "tests", "fixtures", "final_draft_log.txt")
DEFAULT_NAME = "Replay-2026.9.21-11053-35966161-C03C03C03.txt"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", required=True, help="scratch directory to write into")
    ap.add_argument("--delay", type=float, default=3.0, help="seconds between snapshots")
    ap.add_argument("--name", default=DEFAULT_NAME, help="filename to write")
    ap.add_argument("--start", type=int, default=1, help="first snapshot index (1-based)")
    ap.add_argument("--no-final", action="store_true", help="stop before the completed log")
    args = ap.parse_args()

    os.makedirs(args.dest, exist_ok=True)
    existing = [f for f in os.listdir(args.dest) if f != args.name]
    if existing:
        print(f"refusing: {args.dest} contains other files ({existing[:3]}...). "
              "Use an empty scratch directory, never the real MTGO log folder.",
              file=sys.stderr)
        return 2

    snaps = sorted(glob.glob(os.path.join(SNAP_DIR, "snap_*.txt")))[args.start - 1:]
    if not args.no_final:
        snaps.append(FINAL)
    dest = os.path.join(args.dest, args.name)
    for i, src in enumerate(snaps):
        shutil.copyfile(src, dest)
        print(f"[{i + 1}/{len(snaps)}] wrote {os.path.basename(src)} -> {dest}")
        if i < len(snaps) - 1:
            time.sleep(args.delay)
    return 0


if __name__ == "__main__":
    sys.exit(main())
