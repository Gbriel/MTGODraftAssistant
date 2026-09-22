/* MTGO Draft Assistant — client. Listens on /events (SSE) and re-renders. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
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
  function cardList(cards, cls) {
    const ul = el("ul", "inline-cards");
    for (const c of cards) ul.appendChild(el("li", cls, c));
    return ul;
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
      head.appendChild(document.createTextNode(` came back. You took `));
      const you = el("strong", null, wheel.your_pick);
      you.style.color = "var(--mine)";
      head.appendChild(you);
      head.appendChild(document.createTextNode(`; the pod took ${wheel.taken.length}:`));
      note.appendChild(head);
      note.appendChild(cardList(wheel.taken, "gone"));
    }

    cur.cards.forEach((c, i) => {
      const li = el("li");
      li.appendChild(el("span", "n", String(i + 1)));
      li.appendChild(el("span", null, c));
      list.appendChild(li);
    });
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
      b.appendChild(cardList(f.passed, "flight"));
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
      b.appendChild(el("div", "label", `pod took ${w.taken.length}`));
      b.appendChild(cardList(w.taken, "gone"));
      b.appendChild(el("div", "label", `came back ${w.returned.length}`));
      b.appendChild(cardList(w.returned, "back"));
      if (w.warning) {
        const warn = el("div", "label", w.warning);
        warn.style.color = "#f0c890";
        b.appendChild(warn);
      }
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

    const byPack = new Map();
    for (const p of done) {
      if (!byPack.has(p.pack)) byPack.set(p.pack, []);
      byPack.get(p.pack).push(p);
    }
    for (const [pack, picks] of [...byPack.entries()].sort((a, b) => b[0] - a[0])) {
      const g = el("div", "pack-group");
      g.appendChild(el("h3", null, `Pack ${pack} · ${picks.length} picks`));
      const ol = el("ol", "cards");
      for (const p of picks.slice().reverse()) {
        const isNewest = flashNew && p === done[done.length - 1];
        const li = el("li", "mine" + (isNewest ? " flash" : ""));
        li.appendChild(el("span", "n", String(p.pick)));
        li.appendChild(el("span", null, p.picked));
        li.appendChild(el("span", "n", `${p.available.length}`));
        li.title = `${p.available.length} cards in pack:\n${p.available.join("\n")}`;
        ol.appendChild(li);
      }
      g.appendChild(ol);
      box.appendChild(g);
    }
  }

  function render(st) {
    renderHeader(st);
    renderCurrent(st);
    renderInFlight(st);
    renderWheels(st);
    renderPicks(st);
  }

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
