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
pytest                          # replay tests must pass
```

## Status

- `src/mtgo_draft_assistant/draft_log.py` — parser, written and verified
- `tests/test_replay.py` — live-tailing regression suite, passing
- Everything else in `DESIGN.md` §4 — not started. Milestones in §5.

Start at M1. Don't skip ahead to 17Lands or match logs.
