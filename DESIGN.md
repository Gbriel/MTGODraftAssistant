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
- **The card data endpoint the site itself uses (verified 2026-09-25):**
  ```
  https://www.17lands.com/api/card_data?expansion=Cube%20-%20Powered&event_type=PremierDraft&time_period=ALL_TIME
  ```
  Response: `{"copyright", "notes", "data": [card, ...]}`. Found by reading
  the site's bundle: the Card Data page calls `/api/card_data` with
  `expansion`, `event_type`, `time_period`, `user_group`, `colors`.
  `time_period` values come from `/data/filters` (`ALL_TIME`,
  `LATEST_EVENT`, `LAST_TWO_WEEKS`, ...). Pyrogoyf: 77,954 GIH games,
  62.9% GIH WR — exactly what the site shows.
- **The trap: `/card_ratings/data` is the OLD endpoint and is wrong for cube.**
  It takes `expansion`, `format`, `start_date`, `end_date` (plain dates only;
  datetimes are rejected with `date_from_datetime_inexact`), returns the same
  card schema, **ignores the dates entirely for cube**, and serves only a
  short recent slice: 452 GIH games for Pyrogoyf instead of 77,954, and the
  count of cards with a win rate drifted from 6 to 1 in a day. Everything
  §2.4 said on 2026-09-24 about thin samples came from that endpoint and was
  an artefact. Don't use it.
- **Expansion codes are not what you'd guess.** `CUBE` returns `[]`. The real ones:
  - `Cube - Powered` → the **powered/Vintage** cube (contains Black Lotus,
    Ancestral Recall, Time Walk, Moxen) — **this is the one to use**
  - `Cube` → unpowered Arena Cube
  - `Cube - Planar`, `Chaos` → other variants
  Note the space must be percent-encoded.
- **Card fields:** `name`, `mtga_id`, `color`, `rarity`, `url`, `types`,
  `seen_count`, `avg_seen` (ALSA), `pick_count`, `avg_pick` (ATA),
  `game_count`, `win_rate` (GP WR), `opening_hand_win_rate`,
  `drawn_win_rate`, **`ever_drawn_win_rate` (GIH WR)**, `ever_drawn_game_count`,
  `never_drawn_win_rate`, `drawn_improvement_win_rate` (IWD).
- **What ALL_TIME looks like (2026-09-25):** 952 cards, because it spans
  every run of the powered cube and retired cards stay in; 783 have a GIH WR.
  Win rates are null only when the game count is tiny (lowest with a value:
  512). GIH WR deciles run 50.5% to 58.4%; the top of the cube sits near 63%.
  Always render the game count next to the rate.
- **Usage notice.** The response's `notes` field says the data is only for use
  on 17Lands.com and that the only data permitted for outside use is at
  17lands.com/public_datasets. This tool is a private local reader making one
  pull a day, but that is the user's call, not ours; the notice is surfaced
  in the README.
- **Etiquette:** serialise requests, ~1s apart, back off on failure, and **cache to
  disk**. Refresh at most once a day. This is one pull (~650KB); don't
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

### 2.5 Arena draft log

Verified 2026-09-24 against `Player.log` on this machine: six complete Arena
powered-cube drafts (`CubeDraft_Powered_20260908`) and one captured live.
Fixtures extracted verbatim in `tests/fixtures/arena/`.

- **Location:** `%USERPROFILE%\AppData\LocalLow\Wizards Of The Coast\MTGA\Player.log`.
  Draft messages appear only with **Options → Account → Detailed Logs (Plugin
  Support)** on; the file then says `DETAILED LOGS: ENABLED` near the top.
  Wizards provides that switch for exactly this purpose, so reading it keeps
  the read-only stance.
- **Rewritten on every client launch** (the old one becomes
  `Player-prev.log`), and it grows large: 98MB / 397k lines after one day.
  Tail it by byte offset. Detect a relaunch as a size shrink, and — because
  a poll can miss the small phase — also by re-reading the last bytes
  consumed and comparing. Do not re-read the whole file per change.
- **Encoding UTF-8, line endings CRLF throughout.** One bare CR exists in
  the Unity boot banner; splitting on LF and stripping CR handles it.
- **The pack on screen:** `[UnityCrossThreadLogger]Draft.Notify {"draftId",
  "SelfPack", "SelfPick", "PackCards": "id,id,..."}`. Card ids are Arena
  grpIds, not names. **`SelfPick` is trustworthy** (unlike MTGO's header):
  `len(PackCards) == 16 - SelfPick` held for all 270 notifies. Keep checking.
- **The pick:** `==> EventPlayerDraftMakePick {"id", "request": "<JSON
  string>"}` — JSON inside JSON, decode `request` again — with `DraftId`,
  `GrpIds` (one id), `Pack`, `Pick`. The response is a separate `<==
  EventPlayerDraftMakePick(id)` line followed by `{"IsPickSuccessful":true}`;
  no failure was seen in 100MB. **The next `Draft.Notify` can be logged
  before the previous pick's response**, so treat the request as the commit.
- **Repeats.** The live capture logged every `Draft.Notify` three times with
  identical content; earlier drafts once. Key on (pack, pick). One draft
  logged two pick requests for P3P15 with different ids; only the second was
  in the notified pack. Rule: a request whose card is not in the pack is
  ignored with a warning; the last valid one wins.
- **Event name:** the `==> EventJoin` request before the draft carries
  `EventName`; the draft id first appears in the first notify, 10–85 lines
  later. `==> DraftCompleteDraft` ends the draft. Timestamps are separate
  `[UnityCrossThreadLogger]M/D/YYYY h:mm:ss AM` lines preceding responses.
- **No player list.** Once in 100MB a pick response carried
  `TableInfo.Players` with the eight screen names; the parser uses it when it
  shows up and never expects it. **Pod size is inferred from the wheels**
  (`analysis.infer_pod_size`: the N for which every pack returns at p + N as
  a subset with N - 1 missing). 21/21 clean pairs at N = 8 in all six drafts;
  before the first lap the UI assumes 8 and says so.
- **Naming ids:** 17Lands rows carry `mtga_id`, which named 482 of 506 ids
  seen. The rest are older printings the cube uses; Scryfall
  `GET /cards/arena/{id}` resolves those (its `/cards/collection` endpoint
  does **not** accept `arena_id` identifiers). Unresolved ids are shown as
  `#<id>` until they resolve, and the same card JSON seeds colours and images.

### 2.6 Arena decks and games

Verified 2026-09-28 against five deck submissions and four matches in real
logs. Fixture: `tests/fixtures/arena/arena_match_opening.log` (the first
turns of one match, verbatim lines).

- **Deck submission:** `==> EventSetDeckV3 {"request": "<json>"}` when you
  submit after building. `EventName` ties it to the draft's event;
  `Deck.MainDeck` and `Deck.Sideboard` are `[{"cardId", "quantity"}]`. It is
  logged once per submission, not per game, so a sideboarded game 2 shows
  cards "seen but not in the submitted main deck" rather than a new list.
- **Match lifecycle:** a JSON line with `matchGameRoomStateChangedEvent`:
  `stateType` `MatchGameRoomStateType_Playing` then `..._MatchCompleted`;
  `gameRoomConfig.reservedPlayers[]` has `playerName` and `systemSeatId`,
  `finalMatchResult.resultList` the game results.
- **Game state:** JSON lines with `greToClientEvent.greToClientMessages[]`;
  type `GREMessageType_GameStateMessage` carries `gameStateMessage`.
  `GameStateType_Full` at `GameStage_Start` (zones present, empty), then
  `GameStateType_Diff`. **A diff's `zones` carry the complete
  `objectInstanceIds` of every changed zone**, and `gameObjects` the changed
  objects, with `grpId` only when visible to you. `gameInfo.stage` turns
  `GameStage_GameOver`. `turnInfo.turnNumber`, `players[].lifeTotal`.
- **Your seat:** the owner of the hand zone whose objects have a `grpId`;
  the opponent's hand lists ids with no objects. Same seat as the message's
  `systemSeatIds`. Verified on matches where the account sat in seat 1 and
  in seat 2.
- **Ids change on zone moves:** `AnnotationType_ObjectIdChanged` with
  `orig_id` / `new_id` details. Reading zone membership and looking objects
  up by id is what makes the count right; a cumulative object map would
  double count.
- **Library is hidden**, so "still in your library" = submitted main deck
  minus your cards in hand, battlefield, graveyard, exile and stack. Checked
  on all four games: library size plus cards seen was exactly 40 every time.
- **Size:** 1.2–2.5 MB of log per match. Parsing stays incremental.

### 2.7 Arena card database

`C:\Program Files\Wizards of the Coast\MTGA\MTGA_Data\Downloads\Raw\Raw_CardDatabase_<hash>.mtga`
is SQLite; the hash changes each client update (glob, newest mtime wins).
`Cards.GrpId` is the log's id; `Cards.TitleId` joins
`Localizations_enUS.LocId` with `Formatted = 1` for the English name, which
may contain `<nobr>` markup. 27,071 cards on 2026-09-28. **It names ids
Scryfall's `/cards/arena/{id}` does not**: Arena-only printings such as
expansion code `ANA` (id 101033 = Dismember), 45 of them in one cache. It
is the first source for names; 17Lands and Scryfall follow. Read-only.

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
    arena_log.py     # Arena Player.log parser + tail watcher -> same Draft — DONE
    arena_game.py    # Arena deck submissions + live game state (library tracker) — DONE
    arena_cards.py   # Arena's own card database: id -> name, offline — DONE
    watcher.py       # poll the draft dir, emit state on change — DONE (M1)
    analysis.py      # wheel diff, in-flight, pod size inference — DONE (M2)
    pool.py          # per-card state from your seat — DONE (M3)
    cube_list.py     # cube lists from file / URL table / 17Lands — DONE (M3)
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
   **Done 2026-09-24.** Lists come from a file, the mtgo.com cube page, or
   17Lands (Arena). The tracker reports seen-but-not-listed cards as the
   signal that the list is wrong.
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
- **Three drafts' worth of MTGO evidence.** §2.2 was verified on one draft and
  re-confirmed on two more (`tests/fixtures/drafts/`, `test_corpus.py`), all
  8-player Holiday 2013 Cube. A different pod size, a set draft, or a
  different cube may still behave differently. Arena: six complete drafts plus
  one live, all 8-player powered cube.

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
