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
  // names are rendered in the order given, i.e. as MTGO listed them
  function tileRow(names, extraCls, titleFor) {
    const row = el("div", "tilerow");
    for (const n of names) row.appendChild(tile(n, "sm", extraCls, titleFor && titleFor(n)));
    return row;
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
    $("file").textContent = st.file ? `${st.log_dir}\\${st.file}` : `watching ${st.log_dir} — no .txt logs yet`;
    $("updated").textContent = st.updated_at ? `updated ${st.updated_at.replace("T", " ")}` : "";
  }

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
      head.appendChild(document.createTextNode(` came back. You took the outlined card; the pod took ${wheel.taken.length}.`));
      note.appendChild(head);
      const row = el("div", "tilerow");
      row.appendChild(tile(wheel.your_pick, "sm", "mine", "your pick"));
      row.appendChild(el("span", "sep"));
      for (const n of wheel.taken) row.appendChild(tile(n, "sm", "", "taken by the pod"));
      note.appendChild(row);
    }

    for (const c of cur.cards) list.appendChild(tile(c, "lg"));
  }

  // ------------------------------------------------------------ in flight
  function renderInFlight(st) {
    const box = $("inflight");
    clear(box);
    $("inflight-count").textContent = st.in_flight.length ? String(st.in_flight.length) : "";
    $("inflight-empty").textContent = st.in_flight.length ? "" :
      (st.current_pack ? "Nothing you passed is due back." : "");
    for (const f of st.in_flight) {
      const b = el("div", "block");
      const head = el("div", "head");
      head.appendChild(el("span", "pos", pos(f.pack, f.first_pick)));
      head.appendChild(el("span", "due",
        `back at ${pos(f.pack, f.due_pick)} · in ${f.picks_until_return} pick${f.picks_until_return === 1 ? "" : "s"}`));
      b.appendChild(head);
      const row = el("div", "tilerow");
      row.appendChild(tile(f.your_pick, "sm", "mine", "your pick"));
      row.appendChild(el("span", "sep"));
      for (const n of f.passed) row.appendChild(tile(n, "sm", "", "passed on"));
      b.appendChild(row);
      box.appendChild(b);
    }
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
      const top = el("div", "tilerow");
      top.appendChild(tile(w.your_pick, "sm", "mine", "your pick"));
      top.appendChild(el("span", "sep"));
      top.appendChild(el("div", "label gone", `pod took ${w.taken.length}`));
      b.appendChild(top);
      b.appendChild(tileRow(w.taken, "", () => "taken by the pod"));
      b.appendChild(el("div", "label back", `came back ${w.returned.length}`));
      b.appendChild(tileRow(w.returned, "", () => "came back to you"));
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
    renderCurrent(st);
    renderInFlight(st);
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
