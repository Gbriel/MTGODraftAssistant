# MTGO Draft Assistant

Live draft tracker for **Magic Online** cube drafts. Reads the draft log MTGO
writes to disk while drafting and surfaces wheel diffs, in-flight packs, and pool
state.

**Read `DESIGN.md` before writing code.** It contains empirically verified facts
about the MTGO log format — including several traps that look like bugs in your
code but are bugs in MTGO's output. Don't re-derive them, and don't assume the
obvious reading of the format is correct.

## The three things that will waste your time if you don't know them

1. **The pick number in `Pack N pick M:` is garbage** — erratic, not off-by-one.
   Ignore it. The *pack* number is fine. Derive pick position by counting.
2. **The last block in a live file has no picked card** — that's the pack on
   screen, not a parse error. Never count it as a committed pick.
3. **Draft logs are UTF-8**, not latin-1. Older community tools get this wrong.

## Working agreements

- **Read-only with respect to MTGO.** Never write to, inject into, or automate the
  client. Only ever read files it already wrote. This is non-negotiable and
  permanent — it's what keeps the tool defensible.
- **Never edit files under the MTGO log directories.** They are inputs. Copy to
  `tests/fixtures/` if you need a new fixture.
- **Keep `tests/test_replay.py` green.** It replays 16 real snapshots of a draft
  log mid-write. If a parser change breaks it, the parser is wrong. Add fixtures;
  don't edit existing ones to fit new code.
- **Small commits, tests alongside.** `pytest` before declaring anything done.
- Prefer stdlib. Add a dependency only when it earns its place.
- Don't hardcode pod size (8) or pack size (15) — read pod size from the
  `Players:` block, derive pack size from the pick-1 card count.
- Don't hardcode the MTGO AppData deployment hash path; glob for it and pick the
  live deployment by mtime. It changes on every client update.

## Running

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
pip install -e .                # makes `python -m mtgo_draft_assistant` work
pytest                          # replay tests must pass
```

Then, with `config.toml` holding `log_dir` (copy `config.example.toml`):

```bash
python -m mtgo_draft_assistant                 # opens http://127.0.0.1:8765/
python -m mtgo_draft_assistant --log-dir C:\path --port 8765 --no-browser
```

To watch the UI update without being in a draft, replay the fixtures into an
empty scratch directory and point the server at it:

```bash
python tools/replay.py --dest C:\some\empty\dir --delay 3
python -m mtgo_draft_assistant --log-dir C:\some\empty\dir
```

## Status

- **M1 done** — `watcher.py` (poll by size+mtime, newest `.txt` wins),
  `server.py` (stdlib `http.server`, JSON at `/api/state`, SSE at `/events`),
  `web/` (single page, no build step), `__main__.py` CLI, `config.py`.
- **M2 done** — `analysis.py`: wheel diffs and in-flight packs. Pod size from
  `Players:`, pack size derived per pack from the first block seen.
- **Card colours + images done** — `scryfall.py`: lazy per-card lookup as
  names appear in the log, throttled to one request per 100ms, cached forever
  in `data/cache/cards.sqlite` with small JPEGs in `data/cache/images/`. No
  bulk download, nothing large held in memory. `--no-cards` runs offline.
  UI shows colour-coded cells (black text) grouped W U B R G / multi /
  colourless / land, images on the pack on screen, hover preview elsewhere.
- **M4 17Lands done** — `ratings.py`: one daily pull of the Arena
  `Cube - Powered` card ratings, cached in `data/cache/ratings.sqlite`,
  joined by name (front-face and accent-insensitive fallbacks). The badge on
  every tile is **ALSA** (average last-seen-at), because win rates are `null`
  below 500 games and cube data is thin; GIH WR is added when it exists. The
  status line reports dataset size, how many cards have a win rate, and how
  many cards seen this draft have no Arena data. `--no-ratings` or a
  `[ratings]` table in `config.toml` (see `config.example.toml`) controls it.
  DESIGN.md §2.4 records what the endpoint really does; read it before
  touching the fetch.
- Tests: `test_replay.py`, `test_watcher.py`, `test_analysis.py`,
  `test_scryfall.py`, `test_ratings.py`, `test_server.py`, `test_config.py` —
  all passing, no network needed.
- **Next: M3** pool tracker (needs a cube list). M5 match logs last, after
  re-verifying the format.

The server is stdlib on purpose (one page, one stream, one local client); a
framework has not earned its place yet. Revisit if the API grows.
