# MTGO Draft Assistant

A live, read-only tracker for **Magic Online** cube drafts (and Arena drafts,
see below). While you draft, it reads the draft log MTGO writes to disk and
shows, in a browser tab:

- **The pack on screen**, as card images in the order MTGO lists them.
- **Wheels**: for every pack that came back to you, which cards the pod took
  (red X) and which wheeled.
- **In-flight packs**: what you passed that hasn't returned yet, when it's due,
  and how many cards it will have.
- **A colour tally** of what the pod has taken, with the share of each colour
  that wheeled.
- **Find a card**: a search box that says whether a card has been seen or
  picked yet (see below).
- **Your picks**, in a collapsible drawer.
- **17Lands numbers** on every card the Arena cube shares with yours (see
  below).

It never writes to, injects into, or automates the MTGO client. It only reads a
text file MTGO already wrote.

The page has one status line at the top (position, state, cube or set,
connection) with a gear for settings: the 17Lands toggles, the wheel tally
options and the MTGO log folder. The draft view shows the pack in front of you,
a diagram of where every pack in the pod is (hover a seat to see that pack),
your picks, and the packs that have wheeled. During an Arena draft a
**Deck & game** link switches to the decklist and library tracker.

## Setup

You need **Python 3.11 or newer** ([python.org](https://www.python.org/downloads/);
tick "Add python.exe to PATH" in the installer).

1. **Turn on draft logging in MTGO.** Settings → tick **Save Draft Log** and note
   the folder path shown next to it.
2. **Get the code.** Clone this repo or download it as a zip and unzip it.
3. **Run it.** On Windows, double-click `run.bat`. It creates a virtual
   environment on first run, installs the project, starts the server and opens
   `http://127.0.0.1:8765/` in your browser.

   Or by hand:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -e .
   python -m mtgo_draft_assistant
   ```
4. **Point it at your log folder.** The first time, the settings panel (the
   gear at the top right) opens by itself. Paste the folder from step 1 into
   **MTGO draft log folder** and press Save. It's remembered in `config.toml`.

The same panel shows, for each client, whether its log was found and what to
do if not: for MTGO, turn on Save Draft Log and paste the folder; for Arena,
turn on Options → Account → Detailed Logs (Plugin Support) and restart Arena.
Arena's log is always at the same place for a normal install, which is filled
in for you; the field is editable for unusual installs, with a warning, and
**Use default** puts it back. When something is wrong the gear shows a gold
mark and the status pill says "check settings".

Leave it running while you draft. The page updates itself after each pick and
resets when a new draft starts.

## Card images

Card colours and images come from [Scryfall](https://scryfall.com), fetched
lazily as cards appear and cached forever in `data/cache/`. The first draft with
a new cube takes a minute or so to fill in; after that it's instant. Run with
`--no-cards` for a text-only offline mode.

## Find a card

Type a card name into the **Find a card** box to see whether it has been seen
this draft and what happened to it: you picked it, it is on screen, you passed
it and it is due back at a given pick, or the pod took it. The line next to the
box counts cards seen so far.

Searching for cards you have *not* seen needs a cube list. Put one in
`config.toml`:

```toml
[cube]
list = "https://www.mtgo.com/vintage-cube-cardlist"   # or a text file, one card per line
```

or pass `--cube PATH_OR_URL`. A URL is fetched once and cached for a week. In
`--arena` mode the 17Lands card list is used automatically. Without a list,
only cards that have already appeared can be found.

## Deck and games on MTGO

MTGO never writes your registered deck to disk, so the Deck pane needs one
step from you: after building, in MTGO's deck editor choose **Export** and
save the `.dek` file into your draft log folder (the one in settings). The
app picks up the newest `.dek` written after the draft started and shows it.

During matches it reads MTGO's own match log: the turn, the score, your cards
as you play them, and the opponent's cards as they show them. MTGO does not
log draws, hands or life totals, so **Not yet seen** is your deck minus the
cards you have played, not a true library count. Sideboarding is not logged
either: drag a card between the deck and the sideboard row on the page to
record it. The edit applies to the current match and is forgotten when the
next match starts; **undo all** clears it sooner.

## MTG Arena

The same tracker works for **Arena** drafts:

```
python -m mtgo_draft_assistant --arena
```

Arena must have **Options → Account → Detailed Logs (Plugin Support)** turned
on; the tool then tails `Player.log` (read-only, like everything else) and shows
the same panes. Arena logs card ids rather than names; they are named from
Arena's own card database on your machine, so it works offline and covers
Arena-only printings. Arena does not log the pod, so the player count is
inferred from the first pack that wheels back and shown as "assumed" until
then. Use `--arena-log PATH` if your log lives somewhere unusual.

**Deck pane.** Once you submit a deck the pane shows your main
deck and sideboard. While a game is running it shows the turn, life totals,
how many cards are in your library, and which cards are still in it, worked
out as your deck minus every card of yours seen in hand, on the battlefield,
in the graveyard or in exile. Hover a card for its chance to be your next
draw. Cards the opponent has shown are listed underneath. If you sideboard
between games, the new cards appear as "seen but not in the submitted main
deck", because Arena logs the deck only once.

## 17Lands ratings

[17Lands](https://www.17lands.com) publishes card statistics for **Arena**
formats. For an Arena draft the tool picks the dataset from the event you are
in: a set draft such as `PremierDraft_FRA_20260929` loads that set, a cube
draft loads `Cube - Powered`. There is no MTGO data, but the Arena powered cube
overlaps the MTGO Vintage Cube heavily, so MTGO drafts use the `[ratings]`
expansion from `config.toml`, `Cube - Powered` by default. Each dataset is
about 650KB, cached in `data/cache/`, and joined by card name. The numbers are
the same ones the 17Lands Card Data page shows.

17Lands regenerates its stats once a day, starting at 00:00 UTC and taking a
few hours. The tool re-downloads when a new UTC day begins and, if the numbers
come back unchanged because the generation hasn't finished, tries again every
three hours until they do, so you get each day's update within a few hours of
it appearing on the site. The 17Lands line says when the next check is due.

Each card gets a small badge: its **GIH WR** (games-in-hand win rate: how often
decks won when they had the card in hand at some point), then its **ALSA**
(average last-seen-at: the pick at which the pod, on average, last saw the card
before someone took it; 1.5 means first-picked, 9 means it wheels). Gold marks
the top of the cube by GIH WR, green the next tier. Hover a card for the rest,
including the game counts. A card with fewer than 500 games shows no win rate.

The line under the header says how many cards the dataset has, how many have a
win rate, and how many cards you have seen this draft have no Arena data at all
(hover it for the list). Untick **badges** to hide them.

Two toggles on the 17Lands line pick the dataset. **top players** switches
every number to 17Lands's top-player group, the same as the site's "Top" user
filter. **latest event only** uses just the current cube run instead of every
run 17Lands has recorded, so a card's number reflects the cube as it is now.
All four combinations are downloaded once a day, so switching is instant.

Run with `--no-ratings` to skip this, or set `enabled = false` under
`[ratings]` in `config.toml`; the same table can pick another expansion
(`"Cube"` is the unpowered Arena cube), format, time period, refresh interval
or game threshold. See `config.example.toml`.

**A note on terms.** The endpoint that serves these numbers is the one the
17Lands website uses for its own pages, and its response carries a notice that
the data is for use on 17Lands.com, with their public datasets offered for
outside use. This tool reads it once a day for one person's private screen;
whether that is acceptable to you is your decision. `--no-ratings` turns the
whole feature off.

## Options

```
python -m mtgo_draft_assistant --help
  --log-dir DIR             draft log folder (otherwise config.toml or the web page)
  --arena                   watch MTG Arena's Player.log instead
  --arena-log PATH          where that log is, if not the default
  --cube PATH_OR_URL        cube card list for the pool tracker
  --port 8765               local port
  --no-browser              don't open a tab
  --no-cards                skip Scryfall; names only
  --no-ratings              skip 17Lands
  --ratings-expansion NAME  17Lands expansion (default "Cube - Powered")
  --ratings-format NAME     17Lands format (default "PremierDraft")
  --cache-dir DIR           where card data, images and ratings are cached
```

## Development

```
pip install -r requirements.txt
pytest
```

`DESIGN.md` documents the MTGO log format, including several traps that look like
parser bugs but are quirks of MTGO's output. Read it before touching the parser.
`tests/fixtures/` holds a real draft log captured mid-write, which the tests
replay.
