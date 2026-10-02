# MTGO Draft Assistant — UX redesign brief

## What this is

A single-page web app, opened in a browser tab on a second monitor (or beside
the game window) while a person drafts Magic: The Gathering on **Magic Online
(MTGO)** or **MTG Arena**. It reads the game client's log files and shows what
the player cannot easily hold in their head during a 45-pick draft. It is
read-only: it never acts on the game, it only displays information.

The person glances at it for a few seconds between picks, under time
pressure (a pick timer of roughly 30–60 seconds). Everything important must be
readable at a glance from arm's length; secondary detail belongs behind hover
or a collapsed section. Dark theme is expected (the game clients are dark).

## Hard constraints

- **One static page**: `index.html`, `app.js`, `style.css`. No build step, no
  framework, no bundler, no npm. Vanilla JavaScript and CSS only. External
  assets must not be required (the tool must work offline except for data it
  has already cached).
- **The data contract is fixed.** The server pushes a JSON "state" object
  over Server-Sent Events (`/events`) on every change, and the same object is
  available at `GET /api/state`. A sample is in `docs/sample-state.json`.
  The redesign can lay the information out however it likes but must render
  from that object. Field names are described below.
- **Card images** are small JPEGs (146×204) served at `/img/<id>.jpg`, keyed
  per card in the state; many cards have no image yet when a draft starts
  (they arrive over the following seconds), so every card needs a text
  fallback: name in a colour-coded cell (white/blue/black/red/green/
  multicolour/colourless/land) that is readable with **black text**.
- **Updates are frequent and partial**: a new pack appears every 30–60
  seconds, card images and ratings trickle in and re-render the page several
  times per minute. Re-rendering must not lose scroll position or the open/
  closed state of sections, and must not flicker.
- Typical viewport: 1300–1900 px wide, 900–1100 px tall, often not full
  screen. Must degrade to a single column below ~900 px.
- Everything must keep working with keyboard-free, mouse-only use. No
  modals, no multi-step flows. Hover tooltips are fine; the current build uses
  the browser's native `title` tooltips, which a redesign may replace with
  custom ones.
- There is no login, no settings page beyond what is listed below, no
  multiple users.

## The panes and what each must show

Priorities: **P0** must be visible without interaction; **P1** visible but can
be compact or secondary; **P2** can live behind a hover, toggle or collapsed
section.

### 1. Header / status strip (P0)

- Which client the draft comes from (MTGO or Arena), the cube or set name,
  the number of players in the pod and whether that number is known from the
  log, inferred from the packs, or assumed (`draft.pod_size_source`), and for
  Arena pick-two drafts "2 cards per pick".
- Current position: pack and pick number, and one of three states: a pack is
  **on screen** (the player is choosing now), **waiting** for the next pack,
  or **idle** (no draft). The pack-on-screen state is the one that matters.
- Connection status (live / reconnecting), background activity ("fetching 12
  cards…"), and any errors, which must show card **names**, not ids.
- Which file/folder is being watched (footer-level detail, P2) and a way to
  change the MTGO log folder (a text field + Save, shown only when needed or
  on request).

### 2. The pack on screen (P0, the most important thing on the page)

- The cards in the pack in front of the player right now, as images, **in the
  order the client lists them** (people cross-reference against the game
  window, so the order must match).
- On each card, two numbers from 17Lands (see "Ratings"): **games-in-hand win
  rate** first, then **average last-seen-at pick**. A visual tier (top of the
  format / next tier / rest) derived from the win-rate percentile. These must
  be readable on a 66-px-wide card today; a redesign may choose a larger
  card size for this pane only.
- If this pack has **wheeled** (it was in front of the player 8 picks ago):
  which cards the pod took out of it in the meantime, shown **below** the
  cards that came back, and the card the player took from it the first time.

### 3. Coming up (P1)

For the rest of the current pack round, pick by pick: the packs the player
has already seen and passed (what they passed, and how many of those cards
will still be there when it returns), and which picks will be packs they have
never seen. Each listed pack shows the player's own pick from it, set apart.

### 4. Wheels (P1)

For every pack that has come back to the player: the player's original pick,
then the cards that came back, then the cards the pod took (currently a red
X). Newest first. A one-line legend. A summary line above everything: how
many cards of each colour the pod has taken, and what share of each colour
wheeled, with toggles to include/exclude multicolour cards and lands.

### 5. 17Lands line and controls (P1)

- Which dataset is loaded (expansion, event type, all-time or latest event,
  all players or top players), how many cards it has and how many have a win
  rate, when it was fetched and when the next check is due.
- How many cards seen this draft have no data (with the list on hover).
- Four controls: show/hide the badges on cards; **top players** on/off;
  **latest event only** on/off; both of the latter disabled with a reason
  ("loading…", "failed, retry in 40s") until their data has loaded.
- A legend for the badge tiers.

### 6. Find a card (P1)

A text box. Typing two or more letters lists matching cards with one line of
status each: *you picked it at P1P3*; *on screen now*; *seen P2P4, passed, due
back at pick 11*; *seen P2P4, the pod took it*; *not seen yet*. Next to the
box: how many cards have been seen so far and, when a card list for the
format is loaded, how many have not been seen. This replaced a busier
"pool" view with tiles and filters that the owner rejected as too much; keep
it a lookup, not a dashboard.

### 7. Deck (Arena only, P1, collapsed by default until a game starts)

- After the player submits a deck: the main deck and sideboard as cards,
  grouped by colour and sorted by mana value, with a count badge on
  duplicates (basic lands).
- During a game: opponent's name, game number, turn, both life totals, how
  many cards are left in the player's library, and **which cards are still in
  the library** with each card's chance of being the next draw. Cards seen
  that were not in the submitted deck (sideboarded in) flagged separately.
  Cards the opponent has shown, in their own row.

### 8. Your picks (P2, collapsed by default)

Every card the player has taken, grouped by pack, in pick order; in a
pick-two draft both cards of a pick.

### Hover preview (P2)

Hovering any card anywhere shows a larger image (260 px wide) near the
cursor. Keep this; it is how people read card text.

## The ratings (what the numbers mean)

From 17Lands, Arena-only data:

- **GIH WR** (games-in-hand win rate): share of games won by decks that had
  the card in hand at some point. Typical range 45–65%; the top of a format
  is ~60%+. Shown as a percentage with one decimal. Null below 500 games.
- **ALSA** (average last-seen-at): the average pick at which the card was
  last seen before someone took it. 1.5 means first-picked; 9+ means it
  usually wheels. Shown with one decimal.
- Percentiles within the dataset (`gih_pct`, `alsa_pct`, 0–100) drive the
  tier colour. Current mapping: ≥85 top tier, ≥55 middle, else low.
- Hover detail: ALSA, ATA (average taken-at), GIH WR with its game count, GP
  WR, OH WR, IWD, play rate, and how many times the card was seen.

The owner cares most about **GIH WR**; it should be the most legible number.

## Visual language today (free to change)

Dark background (#12141a), panes (#1a1d25), accent blue, gold = "your pick",
red = "taken by the pod", green = "came back to you", purple = "in flight".
Card tiles are 66 px wide everywhere except the hover preview. Monospace for
positions like `P1P7`. Colour-coded text cells for cards without images use
pale tints with black text. Markers drawn over card images: a gold star for
the player's pick, a red X for cards the pod took, a green ring for cards that
wheeled back, a dashed purple ring for in-flight.

## What to deliver

Replacement `index.html`, `app.js`, `style.css` that render from the state
object described in `docs/sample-state.json`, honouring the constraints
above, or a design spec and mockups precise enough to implement them.
Keep the element ids used for state-dependent text if possible; a full
restructure is acceptable as long as every item in the pane list above is
still shown at its priority.

## State object: the fields the page uses

Top level: `version`, `updated_at`, `source` (`mtgo` | `arena` | `auto`),
`log_dir`, `log_dir_ok`, `file`, `arena_log`, `config_locked`, `ui_version`
(reload the page when it changes), `warnings[]`, `cards_pending`,
`card_errors[]`.

`draft`: `event_id`, `timestamp`, `hero`, `players[]`, `set_name`,
`pod_size`, `pod_size_source`, `pack_sizes{pack: size}`, `cards_per_pick`,
`source`.

`position`: `pack`, `pick`, `status` (`on_screen` | `waiting` | `idle`).

`current_pack`: `pack`, `pick`, `cards[]` (names, in client order) or null.

`picks[]`: `pack`, `pick`, `picked`, `picked_all[]`, `available[]`,
`complete`.

`wheels[]`: `pack`, `first_pick`, `return_pick`, `your_pick`, `passed[]`,
`returned[]`, `taken[]`, `warning`.

`in_flight[]`: `pack`, `first_pick`, `due_pick`, `your_pick`, `passed[]`,
`picks_until_return`.

`cards{name}`: `status` (`ok` | `missing`), `group` (W U B R G M C L X),
`colors[]`, `color_identity[]`, `type_line`, `mana_cost`, `cmc`, `image`
(URL or null).

`ratings{name}`: `gih_wr`, `gih_games`, `alsa`, `ata`, `gp_wr`, `games`,
`oh_wr`, `iwd`, `play_rate`, `seen_count`, `pick_count`, `gih_pct`,
`alsa_pct`. `ratings_sets{"PERIOD|group": {name: same}}` for the toggles.
`ratings_meta`: `expansion`, `format`, `time_period`, `user_group`,
`status`, `fetching`, `fetched_at`, `age_hours`, `cards`, `with_win_rate`,
`min_games`, `error`, `retry_in`, `next_check_hours`, `sets{key: same}`.
`ratings_unmatched[]`: names with no data.

`pool`: `cards[]` of `{name, state, pack, pick, due, reason}` with state in
`mine` | `on_screen` | `in_flight` | `gone` | `unseen`; `counts{state}`;
`cube` (`name`, `source`, `size`, `fetched_at`, `unmatched[]`) or null;
`note`.

`arena_game`: `deck` (`event`, `submitted_at`, `main[]`, `side[]` of
`{grp, name, n, named}`, `main_count`, `side_count`) and `game`
(`match_id`, `game_number`, `stage`, `over`, `turn`, `my_seat`, `opponent`,
`life{seat: n}`, `library`, `seat_known`, `remaining[]`, `seen_mine[]`,
`unexpected[]`, `seen_theirs[]`), either may be null.
