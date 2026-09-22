# MTGO Draft Assistant

A live, read-only tracker for **Magic Online** cube drafts. While you draft, it
reads the draft log MTGO writes to disk and shows, in a browser tab:

- **The pack on screen**, as card images in the order MTGO lists them.
- **Wheels**: for every pack that came back to you, which cards the pod took
  (red X) and which wheeled.
- **In-flight packs**: what you passed that hasn't returned yet, when it's due,
  and how many cards it will have.
- **A colour tally** of what the pod has taken, with the share of each colour
  that wheeled.
- **Your picks**, in a collapsible drawer.

It never writes to, injects into, or automates the MTGO client. It only reads a
text file MTGO already wrote.

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
4. **Point it at your log folder.** The first time, the page asks for the folder
   from step 1. Paste it and hit Save. It's remembered in `config.toml`; use
   "change folder" in the page footer to change it later.

Leave it running while you draft. The page updates itself after each pick and
resets when a new draft starts.

## Card images

Card colours and images come from [Scryfall](https://scryfall.com), fetched
lazily as cards appear and cached forever in `data/cache/`. The first draft with
a new cube takes a minute or so to fill in; after that it's instant. Run with
`--no-cards` for a text-only offline mode.

## Options

```
python -m mtgo_draft_assistant --help
  --log-dir DIR     draft log folder (otherwise config.toml or the web page)
  --port 8765       local port
  --no-browser      don't open a tab
  --no-cards        skip Scryfall; names only
  --cache-dir DIR   where card data and images are cached
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
