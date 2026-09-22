/* MTGO Draft Assistant — client. Listens on /events (SSE) and re-renders.
 * Cards are shown as images in the order MTGO listed them in the pack. A
 * card with no image yet falls back to its name in a colour-coded cell
 * (black text; grey until Scryfall data arrives). */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  let cards = {};              // name -> info from the server
  let lastCommitted = -1;      // to flash the newest pick

  // ---------------------------------------------------------------- utils
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function pos(pack, pick) { return `P${pack}P${pick}`; }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function info(name) { return cards[name] || null; }
  function group(name) { const i = info(name); return i ? i.group : "X"; }
  function manaLabel(i) {
    if (!i || !i.mana_cost) return i && i.group === "L" ? "land" : "";
    return i.mana_cost.replace(/[{}]/g, "").replace(/ \/\/ .*/, "");
  }

  // A card cell: the image alone when we have one, otherwise the name in a
  // colour-coded frame (not fetched yet, or Scryfall doesn't know the card).
  //   size: "lg" for the pack on screen, "sm" everywhere else
  //   extraCls: e.g. "mine" (gold outline), "flash"
  //   title: extra tooltip line (pick position etc.)
  function tile(name, size, extraCls, title) {
    const i = info(name);
    const hasImg = !!(i && i.image);
    const t = el("div", `tile ${size} ` + (hasImg ? "has-img" : `g-${group(name)}`)
      + (extraCls ? " " + extraCls : ""));
    t.dataset.card = name;
    const showName = () => {
      if (!t.querySelector(".name")) {
        t.appendChild(el("div", "name", name));
        if (i && (i.mana_cost || i.group === "L")) t.appendChild(el("div", "meta", manaLabel(i)));
      }
    };
    if (hasImg) {
      const img = el("img");
      img.src = i.image;
      img.alt = name;
      img.loading = "lazy";
      img.onerror = () => {
        img.remove();
        t.classList.remove("has-img");
        t.classList.add(`g-${group(name)}`);
        showName();
      };
      t.appendChild(img);
    } else {
      showName();
    }
    const lines = [name];
    if (i && i.type_line) lines.push(i.type_line + (i.mana_cost ? "  " + i.mana_cost : ""));
    if (title) lines.push(title);
    t.title = lines.join("\n");
    return t;
  }
  // "Your previous pick: <card>", set apart from the rest of the row
  function prevPick(name) {
    const box = el("div", "prev");
    box.appendChild(tile(name, "sm", "mine", "your pick from this pack"));
    box.appendChild(el("div", "prev-label", "Your previous pick"));
    return box;
  }
  // [previous pick] | [the pack's cards, wrapping in their own space]
  function packRow(prevName, tiles) {
    const row = el("div", "packrow");
    row.appendChild(prevPick(prevName));
    const cards = el("div", "packrow-cards");
    for (const t of tiles) cards.appendChild(t);
    row.appendChild(cards);
    return row;
  }

  // names are rendered in the order given, i.e. as MTGO listed them
  function tileRow(names, extraCls, titleFor) {
    const row = el("div", "tilerow");
    for (const n of names) row.appendChild(tile(n, "sm", extraCls, titleFor && titleFor(n)));
    return row;
  }

  // ------------------------------------------------------- colour tally
  // Which colour buckets a card falls into. Mono-coloured: its colour.
  // Multicolour: every one of its colours (whole, not fractional). Lands:
  // every colour of their identity. No colours at all: colourless.
  // Multicolour and lands can be excluded with the toggles.
  const tallyOpts = { multi: true, lands: true };
  try {
    const saved = JSON.parse(localStorage.getItem("tallyOpts") || "{}");
    if (typeof saved.multi === "boolean") tallyOpts.multi = saved.multi;
    if (typeof saved.lands === "boolean") tallyOpts.lands = saved.lands;
  } catch (e) { /* ignore */ }

  function colorBuckets(name) {
    const i = info(name);
    if (!i || i.status !== "ok") return ["?"];
    if (i.group === "L") {
      if (!tallyOpts.lands) return [];
      const ci = i.color_identity || [];
      return ci.length ? ci : ["C"];
    }
    if (i.group === "M") return tallyOpts.multi ? i.colors : [];
    return i.colors && i.colors.length ? i.colors : ["C"];
  }
  const TALLY_ORDER = ["W", "U", "B", "R", "G", "C"];

  function renderTaken(st) {
    const box = $("taken");
    clear(box);
    const taken = {}, passed = {};
    let total = 0;
    for (const w of st.wheels) {
      const gone = new Set(w.taken);
      for (const n of w.passed) {
        const isGone = gone.has(n);
        if (isGone) total += 1;
        for (const c of colorBuckets(n)) {
          passed[c] = (passed[c] || 0) + 1;
          if (isGone) taken[c] = (taken[c] || 0) + 1;
        }
      }
    }
    if (!total) return;

    box.appendChild(el("span", "lbl", "pod took"));
    const cellFor = (c, cls, tip) => {
      const t = taken[c] || 0, p = passed[c] || 0;
      const cell = el("span", `cell ${cls}`);
      cell.appendChild(el("small", null, c));
      cell.appendChild(el("b", null, String(t)));
      const pct = p ? Math.round(100 * (p - t) / p) : null;
      cell.appendChild(el("span", "pct", pct === null ? "–" : `${pct}%`));
      cell.title = tip || `${c}: pod took ${t} of the ${p} you passed in wheeled packs; ${pct === null ? "none passed" : pct + "% wheeled back"}`;
      return cell;
    };
    for (const c of TALLY_ORDER) box.appendChild(cellFor(c, `g-${c}`));
    if (passed["?"]) box.appendChild(cellFor("?", "g-X", "cards without Scryfall data yet"));

    const opts = el("span", "opts");
    const mk = (key, label) => {
      const lab = el("label");
      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = tallyOpts[key];
      cb.addEventListener("change", () => {
        tallyOpts[key] = cb.checked;
        try { localStorage.setItem("tallyOpts", JSON.stringify(tallyOpts)); } catch (e) { /* ignore */ }
        renderTaken(st);
      });
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(label));
      return lab;
    };
    opts.appendChild(mk("multi", "multicolor"));
    opts.appendChild(mk("lands", "lands"));
    box.appendChild(opts);
    box.appendChild(el("span", "total", `${total} taken · % = share of that colour that wheeled`));
  }

  // -------------------------------------------------------------- header
  function renderHeader(st) {
    const d = st.draft;
    $("cube").textContent = d
      ? `${d.set_name || "unknown cube"} · Event ${d.event_id || "?"} · ${d.pod_size} players`
      : "";
    const p = st.position;
    const badge = $("position");
    badge.className = "position " + p.status;
    if (p.status === "on_screen") badge.textContent = `Pack ${p.pack} · Pick ${p.pick} — on screen`;
    else if (p.status === "waiting") {
      const size = d && d.pack_sizes[String(p.pack)];
      badge.textContent = size && p.pick > size
        ? `Pack ${p.pack} done — waiting for next pack`
        : `Pack ${p.pack} · Pick ${p.pick} — waiting`;
    } else badge.textContent = st.file ? "no picks yet" : "no draft log";

    const players = $("players");
    clear(players);
    if (d) for (const name of d.players) {
      players.appendChild(el("span", "chip" + (name === d.hero ? " hero" : ""), name));
    }

    const w = $("warnings");
    w.hidden = !st.warnings.length;
    w.textContent = st.warnings.join("\n");

    const pend = $("cards-pending");
    pend.textContent = st.cards_pending ? `fetching ${st.cards_pending} cards…`
      : (st.card_errors && st.card_errors.length ? `Scryfall: ${st.card_errors.length} recent errors` : "");
    pend.title = (st.card_errors || []).join("\n");
    if (!st.log_dir) $("file").textContent = "no draft log folder set";
    else if (!st.log_dir_ok) $("file").textContent = `${st.log_dir} — folder not found`;
    else if (st.file) $("file").textContent = `${st.log_dir}\\${st.file}`;
    else $("file").textContent = `watching ${st.log_dir} — no .txt logs yet`;
    $("updated").textContent = st.updated_at ? `updated ${st.updated_at.replace("T", " ")}` : "";
    updateLogDirForm(st);
  }

  // ------------------------------------------------------ log dir form
  const form = $("logdir-form");
  const input = $("logdir-input");
  const msg = $("logdir-msg");
  let editing = false;
  let lastLogDir = null;

  function updateLogDirForm(st) {
    lastLogDir = st.log_dir;
    const needed = !st.log_dir || !st.log_dir_ok;
    form.hidden = !(needed || editing);
    $("logdir-cancel").hidden = needed;
    if (!form.hidden && !input.value && st.log_dir) input.value = st.log_dir;
    if (needed && !editing) {
      msg.className = "logdir-msg err";
      msg.textContent = st.log_dir ? "That folder doesn't exist on this machine." : "";
    }
    const cur = $("current-empty");
    if (needed && !st.picks.length) cur.textContent = "Set the draft log folder above to get started.";
  }
  $("logdir-change").addEventListener("click", (ev) => {
    ev.preventDefault();
    editing = true;
    form.hidden = false;
    $("logdir-cancel").hidden = false;
    input.value = lastLogDir || "";
    msg.textContent = "";
    input.focus();
    input.select();
  });
  $("logdir-cancel").addEventListener("click", () => {
    editing = false;
    form.hidden = true;
  });
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    msg.className = "logdir-msg";
    msg.textContent = "saving…";
    try {
      const r = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ log_dir: input.value }),
      });
      const res = await r.json();
      if (!res.ok) {
        msg.className = "logdir-msg err";
        msg.textContent = res.error || "could not save";
        return;
      }
      msg.className = "logdir-msg ok";
      msg.textContent = `watching ${res.log_dir} · ${res.logs_found} log${res.logs_found === 1 ? "" : "s"} found`
        + (res.newest ? ` · newest: ${res.newest}` : "")
        + (res.warning ? ` · ${res.warning}` : "");
      editing = false;
      setTimeout(() => { if (!editing) form.hidden = !!lastLogDir; }, 2500);
    } catch (e) {
      msg.className = "logdir-msg err";
      msg.textContent = "server not reachable";
    }
  });

  // ------------------------------------------------------------- current
  function renderCurrent(st) {
    const list = $("current-cards");
    const note = $("current-wheel");
    const empty = $("current-empty");
    clear(list);
    note.hidden = true;
    clear(note);
    const cur = st.current_pack;
    if (!cur) {
      $("current-count").textContent = "";
      empty.textContent = st.picks.length
        ? "Waiting for MTGO to write the next pack…"
        : (st.file ? "Draft log found; waiting for the first pack." : "Start a draft in MTGO and the pack will appear here.");
      return;
    }
    empty.textContent = "";
    $("current-count").textContent = `${cur.cards.length} cards · ${pos(cur.pack, cur.pick)}`;

    // If this pack wheeled, say what the pod took out of it.
    const wheel = st.wheels.find((w) => w.pack === cur.pack && w.return_pick === cur.pick);
    if (wheel) {
      note.hidden = false;
      const head = el("div");
      head.appendChild(el("span", "pos", pos(wheel.pack, wheel.first_pick)));
      head.appendChild(document.createTextNode(` came back. The pod took ${wheel.taken.length}:`));
      note.appendChild(head);
      note.appendChild(packRow(wheel.your_pick,
        wheel.taken.map((n) => tile(n, "sm", "gone", "taken by the pod"))));
    }

    for (const c of cur.cards) list.appendChild(tile(c, "sm"));
  }

  // ------------------------------------------------------------- upcoming
  // The rest of this pack, pick by pick. A pick whose pack you passed
  // earlier shows what you passed (N-1 of those will be gone); a pick whose
  // pack you have never held is "unseen".
  function renderUpcoming(st) {
    const box = $("upcoming");
    const head = $("upcoming-head");
    clear(box);
    head.hidden = true;
    $("upcoming-count").textContent = "";
    const p = st.position;
    const d = st.draft;
    if (!d || p.pack === null || p.status === "idle") return;
    const size = d.pack_sizes[String(p.pack)];
    if (!size || p.pick > size) return;                 // pack finished; next pack is all unseen
    const first = p.status === "on_screen" ? p.pick + 1 : p.pick;
    if (first > size) return;

    const byDue = new Map(st.in_flight.map((f) => [f.due_pick, f]));
    const n = d.pod_size;
    let known = 0;
    for (let j = first; j <= size; j++) {
      const f = byDue.get(j);
      if (!f) {
        const row = el("div", "upcoming-row unseen");
        row.appendChild(el("span", "pick", `Pick ${j}`));
        row.appendChild(el("span", "muted", `unseen · ${size + 1 - j} card${size + 1 - j === 1 ? "" : "s"}`));
        box.appendChild(row);
        continue;
      }
      known += 1;
      const b = el("div", "block");
      const h = el("div", "head");
      h.appendChild(el("span", "pick", `Pick ${j}`));
      const remaining = Math.max(0, f.passed.length - (n - 1));
      h.appendChild(el("span", "muted",
        `your ${pos(f.pack, f.first_pick)} pack · ${remaining} of these ${f.passed.length} will be left`));
      b.appendChild(h);
      b.appendChild(packRow(f.your_pick, f.passed.map((c) => tile(c, "sm", "", "passed on"))));
      box.appendChild(b);
    }
    head.hidden = false;
    $("upcoming-count").textContent = `${known} seen · ${size - first + 1 - known} unseen`;
  }

  // --------------------------------------------------------------- wheels
  function renderWheels(st) {
    const box = $("wheels");
    clear(box);
    const wheels = st.wheels.slice().reverse();     // newest first
    $("wheel-count").textContent = wheels.length ? String(wheels.length) : "";
    $("wheels-empty").textContent = wheels.length ? "" : "No pack has come back around yet.";
    for (const w of wheels) {
      const b = el("div", "block");
      const head = el("div", "head");
      head.appendChild(el("span", "pos", `${pos(w.pack, w.first_pick)} → ${pos(w.pack, w.return_pick)}`));
      b.appendChild(head);
      // one wrapping row: your pick (gold), then the pack you passed in its
      // original order, with a red X over every card that did not come back
      const back = new Set(w.returned);
      b.appendChild(packRow(w.your_pick, w.passed.map((n) => back.has(n)
        ? tile(n, "sm", "back", "came back to you")
        : tile(n, "sm", "gone", "taken by the pod"))));
      if (w.warning) b.appendChild(el("div", "warn", w.warning));
      box.appendChild(b);
    }
  }

  // ---------------------------------------------------------------- picks
  function renderPicks(st) {
    const box = $("picks");
    clear(box);
    const done = st.picks.filter((p) => p.complete);
    $("pick-count").textContent = done.length ? String(done.length) : "";
    $("picks-empty").textContent = done.length ? "" : "No picks yet.";
    const flashNew = lastCommitted >= 0 && done.length > lastCommitted;
    lastCommitted = done.length;
    const newest = done.length ? done[done.length - 1].picked : null;

    // one row per pack, in pick order
    const byPack = new Map();
    for (const p of done) {
      if (!byPack.has(p.pack)) byPack.set(p.pack, []);
      byPack.get(p.pack).push(p);
    }
    for (const [pack, picks] of byPack) {
      box.appendChild(el("div", "group-head", `Pack ${pack} · ${picks.length}`));
      const row = el("div", "tilerow");
      for (const p of picks) {
        row.appendChild(tile(p.picked, "sm", flashNew && p.picked === newest ? "flash" : "",
          `picked ${pos(p.pack, p.pick)}`));
      }
      box.appendChild(row);
    }
  }

  // remember whether the picks drawer was left open
  const picksPane = $("pane-picks");
  try { picksPane.open = localStorage.getItem("picksOpen") === "1"; } catch (e) { /* ignore */ }
  picksPane.addEventListener("toggle", () => {
    try { localStorage.setItem("picksOpen", picksPane.open ? "1" : "0"); } catch (e) { /* ignore */ }
  });

  function render(st) {
    cards = st.cards || {};
    renderHeader(st);
    renderTaken(st);
    renderCurrent(st);
    renderUpcoming(st);
    renderWheels(st);
    renderPicks(st);
  }

  // -------------------------------------------------------- hover preview
  const preview = $("preview");
  document.addEventListener("mouseover", (ev) => {
    const t = ev.target.closest("[data-card]");
    if (!t) { preview.hidden = true; return; }
    const i = info(t.dataset.card);
    if (!i || !i.image) { preview.hidden = true; return; }
    preview.src = i.image;
    preview.hidden = false;
  });
  document.addEventListener("mousemove", (ev) => {
    if (preview.hidden) return;
    const w = 260, h = 364, pad = 14;
    let x = ev.clientX + pad, y = ev.clientY + pad;
    if (x + w > window.innerWidth) x = ev.clientX - w - pad;
    if (y + h > window.innerHeight) y = window.innerHeight - h - pad;
    preview.style.left = x + "px";
    preview.style.top = Math.max(0, y) + "px";
  });
  document.addEventListener("mouseout", (ev) => {
    if (ev.target.closest && ev.target.closest("[data-card]")) preview.hidden = true;
  });

  // ------------------------------------------------------------ transport
  function setConn(state, text) {
    $("conn-dot").className = "dot " + state;
    $("conn-text").textContent = text;
  }

  function connect() {
    const es = new EventSource("/events");
    es.addEventListener("state", (ev) => {
      setConn("live", "live");
      try { render(JSON.parse(ev.data)); } catch (e) { console.error(e); }
    });
    es.onopen = () => setConn("live", "live");
    es.onerror = () => setConn("down", "reconnecting…");
  }

  fetch("/api/state").then((r) => r.json()).then(render).catch(() => {});
  connect();
})();
