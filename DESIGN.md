# MTGO Draft Assistant — Design

A live draft tracker for **Magic Online** (not Arena) cube drafts. Reads the draft
log MTGO writes to disk while you draft, and answers three questions the human
brain is bad at during a 45-pick cube draft:

1. **What did the pod take out of this pack while it went around?** (wheel diff)
2. **What's still out there in packs I've seen but haven't got back yet?** (in-flight)
3. **Could card X still show up, or is it already gone?** (pool state)

Everything in the "Verified facts" section below was confirmed empirically against
a real draft on 2026-09-21 (MTGO client `3.4.158.4700`). Don't re-derive it; don't
assume it generalises beyond what's marked verified.

---

## 1. Priorities

| Priority | Feature |
|---|---|
| P0 | Live tail of the draft log; pack-by-pack state |
| P0 | Wheel diff — what the pod took from a pack that came back |
| P0 | In-flight packs — what you passed that hasn't returned yet |
| P0 | Pool tracker — seen / gone / possibly-available, searchable |
| P1 | 17Lands GIH WR alongside each card |
| P2 | Personal per-card win rate (game-in-hand) from your own match history |
| P2 | Your record vs each player in the pod; highlight current opponent |

P2 work depends on parsing MTGO **match** logs, which is a separate and much
messier parsing problem than the draft log. Do not start it until P0 is solid.

---

## 2. Verified facts about the environment

### 2.1 Draft log

- **Written live, append-only, during the draft.** The file appears at pick 1 and
  is updated within seconds of each pick. This is the single fact the whole
  project depends on, and it is confirmed — a 16-snapshot capture of a full
  45-pick draft showed monotonic growth with no retroactive rewriting.
- **Location is user-configured** in MTGO: Settings → "Save Draft Log" + a path
  field. On this machine: `C:\Users\kopit\MTGODraftLogs`.
  The setting is `SaveDraftLog` (bool) and `DraftLogPath` (string), stored under
  `ViewStateSettingsScene` in
  `%LOCALAPPDATA%\Apps\2.0\Data\<hash>\<hash>\mtgo..tion_<hash>\Data\AppFiles\user_settings`.
  MTGO does **not** flush that file promptly, so don't read the setting to find
  the path — ask the user / read it from config.
- **Filename shape:** `<Player>-<Y.M.D>-<EventID>-<n>-<SetCodes>.txt`
  e.g. `Wumpwumpwump-2026.9.21-11053-35966161-C03C03C03.txt`.
  The `EventID` matches the `Event #:` line inside. `C03C03C03` is the three pack
  codes concatenated.
- **Encoding is UTF-8.** Confirmed (`Palantír of Orthanc` round-trips). Older
  third-party tools read MTGO logs as latin-1; that is wrong here and will
  mojibake accented card names. Line endings are **CRLF** — matters if you tail
  by byte offset.

### 2.2 Draft log format — and its landmines

```
Event #: 11053
Time:    9/21/2026 4:07:52 PM
Players:
    Grog_MTG
--> Wumpwumpwump
    Jed_Davies
    ... (8 total)

------ Pack 1: Holiday 2013 Cube ------

Pack 1 pick 1:
    Mother of Runes
    Dark Ritual
--> Broadside Bombardiers
    ... (15 cards)

Picked: Broadside Bombardiers
```

- **The pick number in the header is garbage.** It is not merely off by one; it is
  erratic. Observed in one draft: pack 1 → `pick 1` once then `pick 2` ×14;
  pack 2 → `1`, `2`×10, `3`×4; pack 3 → `1`, `2`×8, … **Ignore the field entirely.**
- **The pack number in the header IS correct.** Use it.
- **Derive pick position by counting blocks within a pack.** Cross-check with the
  invariant `len(available) == packsize + 1 - pick` (15-card packs → `16 - pick`).
  Verified with zero mismatches across 45 picks.
- **The `------ Pack N: <name> ------` separator appears before every pick in
  pack 1 and never again in packs 2 or 3.** Useless as a boundary marker. It is,
  however, the only place the cube/set name appears — capture it once.
- `--> ` marks the picked card. A **completed** pick is additionally followed by a
  `Picked: <card>` line.
- **The final block during a live draft has no `-->` and no `Picked:` line.** That
  is the pack currently on screen, written *before* the human decides. This is
  what makes a live overlay possible — treat it as `current_pack`, never as a
  committed pick.

### 2.3 Match logs (for P2 only)

- `Match_GameLog_<uuid>.dat` and `Match_GameChat_<uuid>.dat`, roughly 1,200
  matches on this machine going back to 2024-05-30.
- Location on this machine:
  `%LOCALAPPDATA%\Apps\2.0\Data\<hashA>\<hashB>\mtgo..tion_<hash>_<version>_<hash>\Data\AppFiles\E719426B979F5236CFCA1211F9B8538E\`
  **Do not hardcode this path.** The deployment hash changes whenever MTGO
  updates. Discover it by globbing
  `%LOCALAPPDATA%\Apps\2.0\Data\**\AppFiles\*\Match_GameLog_*.dat` and taking the
  directory with the most recent files. There are multiple deployment folders
  (old versions); pick the live one by mtime.
- Format markers (from the community `cderickson/MTGO-Tracker` parser — **treat as
  unverified against the current client, re-confirm before relying on them**):
  whole log splits on `@P`; card names wrapped `@[` … `@]`; phrases
  ` joined the game.`, ` begins the game with ` (mulligan count spelled as a word),
  `Turn N: <player>`, ` has conceded`, ` wins the game`. No timestamp inside the
  file — that parser uses file mtime as the match time.

### 2.4 17Lands

Arena data, but **Arena Vintage Cube data does exist** and overlaps MTGO's cube
lists substantially. Expect partial coverage, never full.

- **Filters/vocabulary endpoint:** `https://www.17lands.com/data/filters` → JSON
  with `expansions`, `formats_by_expansion`, `time_periods`, `colors`.
- **Card ratings endpoint:**
  ```
  https://www.17lands.com/card_ratings/data?expansion=Cube%20-%20Powered&format=PremierDraft&start_date=2026-01-01&end_date=2026-09-21
  ```
- **Expansion codes are not what you'd guess.** `CUBE` returns `[]`. The real ones:
  - `Cube - Powered` → the **powered/Vintage** cube (contains Black Lotus,
    Ancestral Recall, Time Walk, Moxen) — **this is the one to use**
  - `Cube` → unpowered Arena Cube
  - `Cube - Planar`, `Chaos` → other variants
  Note the space must be percent-encoded.
- **Response fields** (flat array of objects): `name`, `mtga_id`, `color`,
  `rarity`, `url`, `types`, `seen_count`, `avg_seen` (ALSA), `pick_count`,
  `avg_pick` (ATA), `game_count`, `win_rate` (GP WR), `opening_hand_win_rate`,
  `drawn_win_rate`, **`ever_drawn_win_rate` (GIH WR)**, `ever_drawn_game_count`,
  `never_drawn_win_rate`, `drawn_improvement_win_rate` (IWD).
- **Behaviours that will bite you (re-verified 2026-09-24):**
  1. **Win-rate fields are `null` below 500 games, exactly.** Count fields are
     always populated. Observed: the lowest `ever_drawn_game_count` with a
     non-null GIH WR was 503; the highest with a null was 499. Gate on the
     count and render "insufficient data" rather than a blank or a zero.
  2. **You must pass explicit `start_date`/`end_date`, but they don't filter
     cube data.** Windows of one year, all-time, and ending 2024-12-31 all
     returned the identical 540 cards and 335,656 total games. `/data/filters`
     `start_dates` gives `Cube - Powered` as 2026-08-26: the dataset is the
     **current Arena cube run only**, cumulative. Send `2019-01-01`..today and
     don't build a date picker.
  3. **Sample sizes are thin.** One month into the 2026-08 run: median 237
     GIH games per card, **only 6 of 540 cards had a public GIH WR**. `avg_pick`
     (ATA) was null for 538. `avg_seen` (ALSA) was present for 532 and is the
     one signal that is always usable. Refresh daily; more cards cross 500 as
     the run goes on, and a new run resets everything.
- **Etiquette:** serialise requests, ~1s apart, back off on failure, and **cache to
  disk**. Refresh at most once a day. This is one small pull (~350KB), but don't
  hammer it.
- **Joining:** join on `name`. Normalise `///` → `//` for split/DFC cards and
  strip surrounding whitespace. 17Lands lists DFCs by front face only
  (`Delver of Secrets`, not `Delver of Secrets // Insectile Aberration`) and
  split cards with both halves (`Life // Death`); names keep their accents
  (`Palantír of Orthanc`). Log every unmatched card — with an MTGO cube list
  that differs from Arena's, the unmatched set is real information for the user
  ("55 of 238 cards have no 17Lands data"), not just a warning to swallow.
  Observed on the Holiday 2013 Cube fixture against the 2026-08 Arena run:
  183 of 238 matched.

---

## 3. The analysis, spelled out

Let `N` = pod size, read from the `Players:` block. **Do not hardcode 8**, though 8
is the normal cube pod.

### 3.1 Wheel diff — P0, verified

A booster you see at pick `p` comes back to you at pick `p + N`.

```
passed_at_p   = available(p) - {your_pick(p)}      # what you handed on
returned      = available(p + N)                    # what came back
taken_by_pod  = passed_at_p - returned              # exactly N-1 cards
```

**Verified on the capture: 21/21 wheel pairs matched cleanly, `returned ⊆ passed`
in every case, and `|taken_by_pod|` was always `N-1 = 7`.**

Assert `returned ⊆ passed_at_p`. If it ever fails (odd pod size, a rare
pack size, a bot-filled seat), fall back to subset-matching the returned list
against every earlier pick in the same pack number, and surface a warning rather
than silently mispairing.

This is the single most valuable output in the app: it tells you precisely which
seven cards the pod wanted out of a pack you touched, which is the real signal
read.

### 3.2 In-flight packs — P0

At current pick `p`, any pack first seen at pick `q` where `q + N > p` has not
returned. For each, show what you passed and when it's due back
(`P{pack}P{q+N}`). Once `p` reaches `q + N`, it graduates into a wheel diff.

### 3.3 Pool tracker — P0

The thing worth knowing: **you do not see every card in the draft.** A booster
reaching you at pick `j` has already had `j - 1` cards removed upstream. Over one
round that's `sum(j-1 for j in 1..N)` = **28 unseen cards per round** with `N=8`,
so **84 per draft** — out of 360. Plus the cards you saw and didn't get.

Card states to track per draft:

| State | Meaning |
|---|---|
| `MINE` | you picked it |
| `GONE` | you saw it, it did not wheel back → someone took it |
| `IN_FLIGHT` | you passed it, its pack hasn't returned yet — might still come back |
| `UNSEEN` | never appeared in front of you — in the 84 you never see, in a later round, or in a pack you haven't been passed |

With a cube list loaded, `UNSEEN` is enumerable and the UI can answer "is Sol Ring
still live?" directly. Without one, `UNSEEN` is an unknown set and the UI should
say so honestly rather than implying a card is available.

Cube lists are **singleton** — each card appears at most once per draft — which is
what makes this tractable. Assert it; if a duplicate name shows up, the cube isn't
singleton and the pool logic needs revisiting.

### 3.4 Personal win rate — P2

"Game in hand" WR per card, computed from your own match logs: of games where the
card was in your opening hand or drawn, what fraction did you win. Needs:
draft → deck → matches linkage, plus per-game card-draw events from the match log.
Sample sizes will be tiny (a cube card might have 3 games). **Render the sample
count next to every number and suppress the rate below a threshold** — the same
discipline 17Lands applies at 500 games. A 100% win rate on 2 games is noise, and
presenting it next to a 17Lands number invites the user to read it as comparable.

### 3.5 Pod and opponent records — P2

The `Players:` block gives all N names. Cross-reference against the match log
backlog for head-to-head records. If a match is in progress, surface the current
opponent's record prominently. Opponent names come out of the match log.

---

## 4. Architecture

**Python 3.11+, local web UI in a browser tab.** Decided 2026-09-21: the user
picked browser tab over an always-on-top overlay or TUI, and a **stdlib
`http.server`** over FastAPI (one page, one SSE stream, one local client; no
dependency has earned its place yet).

```
src/mtgo_draft_assistant/
    draft_log.py     # parser — WRITTEN AND TESTED, see tests/
    watcher.py       # poll the draft dir, emit state on change — DONE (M1)
    analysis.py      # wheel diff, in-flight — DONE (M2); pool state — M3
    config.py        # log_dir resolution: --log-dir > env > config.toml — DONE
    server.py        # stdlib http.server: UI, /api/state JSON, /events SSE — DONE
    __main__.py      # CLI entry point — DONE
    ratings.py       # 17Lands fetch + disk cache + name join — DONE (M4)
    scryfall.py      # card colours/type/images — DONE; lazy per-card, SQLite + JPEG cache
                     # (deliberately NOT the bulk file: it is hundreds of MB and the
                     # user asked for low memory; a cube shows a few hundred names)
    matches.py       # P2: match log parsing
    stats.py         # P2: personal WR, pod records
    db.py            # SQLite schema + migrations
web/                 # static single-page UI, no build step — DONE
tools/replay.py      # feed fixture snapshots into a scratch dir to demo live updates
```

**Watching:** poll on a timer (500ms–1s) comparing `(size, mtime)`. Do not use
`watchdog`/inotify-style APIs as the primary mechanism — the log directory may sit
in OneDrive, and network/synced filesystems drop change events. The file is ~10KB;
just re-read and re-parse the whole thing on change. Diff parsed state against the
previous parse to decide what's new. Don't do byte-offset incremental reads — the
complexity buys nothing at this size and CRLF makes offsets error-prone.

**Newest-draft detection:** the log directory accumulates one file per draft. Pick
the most recently modified `.txt`. Treat a *new* filename appearing as "a new draft
started" and reset live state.

**Persistence:** SQLite. Every completed draft gets imported once and kept, so the
pool tracker and personal stats work across drafts. Keep raw log text alongside
parsed rows so reparsing after a parser fix doesn't need the original files.

**UI:** one page, three panes — current pack (with ratings), wheel/in-flight
timeline, searchable pool. Server pushes over SSE. No framework needed; if one is
wanted later, prefer something with no build step.

---

## 5. Milestones

1. **M1 — Live read-only.** Watcher + parser + a page that shows the current pack
   and pick history, updating live. Nothing else. Prove the loop end to end.
2. **M2 — Wheel + in-flight.** The two analyses from §3.1/§3.2. This is the point
   at which the tool becomes worth opening during a draft.
3. **M3 — Pool tracker.** Needs a cube list; until one is loaded, ship the
   seen/gone/in-flight view and be explicit that `UNSEEN` is unknown.
4. **M4 — 17Lands.** Fetch, cache, join, display with sample-size gating and an
   honest unmatched-card count. **Done 2026-09-24**, built before M3 because it
   needs no cube list. ALSA is the badge; GIH WR appears only where 17Lands
   publishes it.
5. **M5 — Match logs.** Re-verify the format markers in §2.3 against a current
   `Match_GameLog_*.dat` before writing any parser. Then personal WR and pod
   records.

---

## 6. Constraints and risks

- **Read-only, always.** Never write to, inject into, or automate the MTGO client.
  The entire defensibility of this tool is that it only reads files MTGO already
  wrote. Screen-scraping, memory reading, and input automation are all out of
  scope permanently, not just for now.
- **Event rules.** Some competitive events prohibit external software. That's the
  user's call to make per event, but don't add anything that automates a decision
  rather than displaying information.
- **The log format is undocumented and can change with any MTGO update.** Parse
  defensively, fail loudly with the offending lines, and keep the fixture corpus
  green as a regression net.
- **One draft's worth of evidence.** Everything verified here comes from a single
  8-player Holiday Cube draft. A different pod size, a set draft, or a different
  cube may behave differently. Capture a second corpus before hardening.

---

## 7. Test fixtures — already in the repo

`tests/fixtures/snapshots/` holds **16 point-in-time captures of one real draft
log as it was being written**, from pick 8 through pick 45. This is the valuable
artifact: it lets you test live-tailing behaviour deterministically, without
drafting.

`tests/test_replay.py` replays them in order and asserts the properties a live
tracker depends on:

1. Committed picks are append-only — a pick never changes in a later snapshot
2. Pick count never regresses
3. The trailing incomplete block is never counted as a committed pick
4. `len(available) == 16 - pick` for every committed pick

**Keep this test green.** If a parser change breaks it, the parser is wrong, not
the test. Add fixtures from new drafts rather than editing existing ones.

`tests/fixtures/final_draft_log.txt` is the same draft, completed — 45 picks,
3 packs, 8 players, Holiday 2013 Cube.
