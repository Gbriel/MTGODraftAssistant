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

- **Read-only with respect to MTGO and Arena.** Never write to, inject into, or
  automate either client. Only ever read files they already wrote. This is
  non-negotiable and permanent — it's what keeps the tool defensible.
- **Never edit files under the MTGO log directories or Arena's `Player.log`.**
  They are inputs. Copy to `tests/fixtures/` if you need a new fixture
  (the Arena fixtures are line extracts, with account inventory data left out).
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
  `Cube - Powered` card data from **`/api/card_data` with
  `time_period=ALL_TIME`** (the endpoint the site itself uses; the older
  `/card_ratings/data` serves a tiny recent slice and made every win rate
  null — DESIGN.md §2.4 has the story), cached in `data/cache/ratings.sqlite`,
  joined by name (front-face and accent-insensitive fallbacks). The badge is
  **GIH WR then ALSA**. A second `LATEST_EVENT` pull defines the current
  Arena cube for the card lookup, since ALL_TIME keeps retired cards.
  `RatingsPool` holds one provider per (expansion, event type, period,
  group) and creates them on demand: an Arena draft's event name picks its
  dataset (`ratings.dataset_for_event`: `PremierDraft_FRA_...` → FRA), MTGO
  drafts use the configured default. Don't pin a single expansion again.
  `--no-ratings` or a `[ratings]` table in `config.toml` controls it. The
  response carries a 17Lands usage notice; it is surfaced in the README and
  is the user's call.
- **Arena done** — `arena_log.py`: streaming parser for `Player.log` draft
  events plus a byte-offset tail watcher, producing the same `Draft` so the
  analysis and UI are shared. `--arena` on the CLI. Card ids are named from
  the 17Lands `mtga_id` column first, then Scryfall `/cards/arena/{id}`
  (cached in `cards.sqlite`). Pod size is inferred from the wheels
  (`analysis.infer_pod_size`) because Arena logs no player list. DESIGN.md
  §2.5 has the verified format, including the repeated-notify and
  duplicate-pick traps. Fixtures in `tests/fixtures/arena/` are verbatim
  extracts of a real log.
- **Arena deck + game tracker done** — `arena_game.py` reads the submitted
  deck (`EventSetDeckV3`) and live game state (`greToClientEvent` zones and
  objects) and computes what is still in your library; the Deck pane shows
  the deck, and during a game the remaining cards with draw odds plus what
  the opponent has shown. DESIGN.md §2.6 has the verified message shapes.
  `arena_cards.py` reads Arena's own card database (read-only SQLite under
  Program Files) and is the **first** source for card names; Scryfall's
  arena-id lookup misses Arena-only printings (§2.7). Any error or unmatched
  line that mentions an id must also carry the name (user request).
- **Second MTGO corpus** — `tests/fixtures/drafts/` holds two more complete
  MTGO logs; `test_corpus.py` runs every invariant over all of them and will
  pick up any new log dropped in.
- Tests: `test_replay.py`, `test_corpus.py`, `test_watcher.py`,
  `test_analysis.py`, `test_arena.py`, `test_scryfall.py`, `test_ratings.py`,
  `test_server.py`, `test_config.py` — all passing, no network needed.
- **M3 done** — `pool.py` puts every card in one of mine / on_screen /
  in_flight / gone (with a reason) / unseen; `cube_list.py` loads a list from
  a text file, a Color|Card HTML table URL (the official MTGO Vintage Cube
  page, cached weekly), or the 17Lands dataset (automatic in `--arena`
  mode). `[cube] list` in `config.toml` or `--cube`. **UI is deliberately
  minimal** (user decision 2026-09-24): a "Find a card" search box that
  answers seen / picked / not seen, plus a seen count. No state grids or
  filters; the user found that version bad. Keep it a lookup.
- **Next: M5** match logs, after re-verifying the format markers in
  DESIGN.md §2.3 against a current `Match_GameLog_*.dat`.

The server is stdlib on purpose (one page, one stream, one local client); a
framework has not earned its place yet. Revisit if the API grows.
