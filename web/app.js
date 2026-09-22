/* MTGO Draft Assistant — client. Listens on /events (SSE) and re-renders.
 * Cards are colour-coded cells (black text) grouped W U B R G, multi,
 * colourless, land; unknown cards are grey until Scryfall data arrives. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const GROUP_ORDER = ["W", "U", "B", "R", "G", "M", "C", "L", "X"];
  const GROUP_LABEL = {
    W: "White", U: "Blue", B: "Black", R: "Red", G: "Green",
    M: "Multicolor", C: "Colorless", L: "Land", X: "Unknown",
  };
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
  function cmc(name) { const i = info(name); return i ? i.cmc : 99; }

  function sortCards(names) {
    return names.slice().sort((a, b) => {
      const ga = GROUP_ORDER.indexOf(group(a)), gb = GROUP_ORDER.indexOf(group(b));
      if (ga !== gb) return ga - gb;
      if (cmc(a) !== cmc(b)) return cmc(a) - cmc(b);
      return a.localeCompare(b);
    });
  }
  function groupBy(names) {
    const out = new Map();
    for (const n of sortCards(names)) {
      const g = group(n);
      if (!out.has(g)) out.set(g, []);
      out.get(g).push(n);
    }
    return out;
  }
  function manaLabel(i) {
    if (!i || !i.mana_cost) return i && i.group === "L" ? "land" : "";
    return i.mana_cost.replace(/[{}]/g, "").replace(/ \/\/ .*/, "");
  }

  // small colour-coded chip; hover shows the image
  function chip(name, extraCls, tag) {
    const c = el("span", `card g-${group(name)}` + (extraCls ? " " + extraCls : ""));
    if (tag !== undefined) c.appendChild(el("span", "tag", String(tag)));
    c.appendChild(el("span", null, name));
    c.dataset.card = name;
    const i = info(name);
    if (i && i.type_line) c.title = `${name}\n${i.type_line}${i.mana_cost ? "  " + i.mana_cost : ""}`;
    return c;
  }
  function chipList(names, extraCls) {
    const ul = el("div", "cardlist");
    for (const n of sortCards(names)) ul.appendChild(chip(n, extraCls));
    return ul;
  }

  // large tile with image
  function tile(name, flash) {
    const i = info(name);
    const t = el("div", `tile g-${group(name)}` + (flash ? " flash" : ""));
    t.dataset.card = name;
    if (i && i.image) {
      const img = el("img");
      img.src = i.image;
      img.alt = name;
      img.loading = "lazy";
      t.appendChild(img);
    }
    t.appendChild(el("div", "name", name));
    const meta = el("div", "meta");
    meta.appendChild(el("span", null, i ? manaLabel(i) : ""));
    meta.appendChild(el("span", null, i && i.type_line ? i.type_line.split(" — ")[0].split(" // ")[0] : ""));
    t.appendChild(meta);
    if (i && i.type_line) t.title = `${name}\n${i.type_line}`;
    return t;
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
      head.appendChild(document.createTextNode(" came back. You took "));
      head.appendChild(el("span", "you", wheel.your_pick));
      head.appendChild(document.createTextNode(`; the pod took ${wheel.taken.length}:`));
      note.appendChild(head);
      note.appendChild(chipList(wheel.taken, "is-gone"));
    }

    for (const c of sortCards(cur.cards)) list.appendChild(tile(c, false));
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
      head.appendChild(el("span", "you", `you took ${f.your_pick}`));
      b.appendChild(head);
      b.appendChild(el("div", "label", `passed ${f.passed.length}`));
      b.appendChild(chipList(f.passed));
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
      head.appendChild(el("span", "you", `you took ${w.your_pick}`));
      b.appendChild(head);
      b.appendChild(el("div", "label gone", `pod took ${w.taken.length}`));
      b.appendChild(chipList(w.taken, "is-gone"));
      b.appendChild(el("div", "label back", `came back ${w.returned.length}`));
      b.appendChild(chipList(w.returned));
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

    const byName = new Map(done.map((p) => [p.picked, p]));
    for (const [g, names] of groupBy(done.map((p) => p.picked))) {
      box.appendChild(el("div", "group-head", `${GROUP_LABEL[g]} · ${names.length}`));
      const list = el("div", "cardlist");
      for (const n of names) {
        const p = byName.get(n);
        const c = chip(n, flashNew && n === newest ? "flash" : "", pos(p.pack, p.pick));
        list.appendChild(c);
      }
      box.appendChild(list);
    }
  }

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
    if (!t || t.classList.contains("tile")) { preview.hidden = true; return; }
    const i = info(t.dataset.card);
    if (!i || !i.image) { preview.hidden = true; return; }
    preview.src = i.image;
    preview.hidden = false;
  });
  document.addEventListener("mousemove", (ev) => {
    if (preview.hidden) return;
    const w = 220, h = 308, pad = 14;
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
