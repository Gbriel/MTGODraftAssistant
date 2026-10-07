/* Draft Assistant — client. Listens on /events (SSE), re-renders from the
 * state object. One page, two views: the draft (default) and the Arena deck /
 * game tracker (#deck). Cards are images when the server has one, otherwise
 * the name in a colour-coded cell with black text. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const DEMO = !!window.DEMO;       // demo.html: render ../data/sample-state.json instead of the server

  let lastState = null, cards = {}, ratings = {}, lastMeta = null, lastCommitted = -1;

  // ------------------------------------------------------------ prefs
  const prefs = { showRatings: true, topPlayers: false, latestEvent: false, picksOpen: false, olderOpen: false, deckMode: "deck", tallyMulti: true, tallyLands: true };
  try { Object.assign(prefs, JSON.parse(localStorage.getItem("draftPrefs") || "{}")); } catch (e) { /* ignore */ }
  function savePrefs() { try { localStorage.setItem("draftPrefs", JSON.stringify(prefs)); } catch (e) { /* ignore */ } }

  // ------------------------------------------------------------ utils
  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  const pos = (a, b) => `P${a}P${b}`;
  const BASICS = { Plains: ["W", "Basic Land — Plains"], Island: ["U", "Basic Land — Island"], Swamp: ["B", "Basic Land — Swamp"], Mountain: ["R", "Basic Land — Mountain"], Forest: ["G", "Basic Land — Forest"] };
  function info(name) {
    if (cards[name]) return cards[name];
    if (BASICS[name]) return { status: "ok", group: "L", colors: [], color_identity: [BASICS[name][0]], type_line: BASICS[name][1], mana_cost: "", cmc: 0, image: null };
    return null;
  }
  const group = (n) => { const i = info(n); return i ? i.group : "X"; };
  const mana = (i) => !i || !i.mana_cost ? (i && i.group === "L" ? "land" : "") : i.mana_cost.replace(/[{}]/g, "").replace(/ \/\/ .*/, "");
  const fmtInt = (n) => n == null ? "?" : Number(n).toLocaleString();
  const fmtPct = (x, d) => x == null ? "n/a" : (100 * x).toFixed(d === undefined ? 1 : d) + "%";
  const fmt1 = (x) => x == null ? "n/a" : Number(x).toFixed(1);

  // ------------------------------------------------------------ 17Lands
  function wantedKey() {
    if (!lastMeta) return null;
    const period = prefs.latestEvent ? "LATEST_EVENT" : lastMeta.time_period, grp = prefs.topPlayers ? "top" : "";
    if (period === lastMeta.time_period && !grp) return null;
    const key = `${period}|${grp}`, s = lastMeta.sets && lastMeta.sets[key];
    return s && s.status === "ok" ? key : null;
  }
  const usingTop = () => { const k = wantedKey(); return !!k && k.endsWith("|top"); };
  const usingLatest = () => { const k = wantedKey(); return !!k && k.startsWith("LATEST_EVENT|"); };
  function setLabel() {
    const period = usingLatest() ? "latest event" : (lastMeta && lastMeta.time_period === "ALL_TIME" ? "all time" : (lastMeta ? lastMeta.time_period : ""));
    return `${period} · ${usingTop() ? "top players" : "all players"}`;
  }
  function ratingLines(name) {
    const r = ratings[name]; if (!r) return [];
    const need = (lastMeta && lastMeta.min_games) || 500, out = [];
    if (r.gih_wr != null) out.push(`GIH WR ${fmtPct(r.gih_wr)} · ${fmtInt(r.gih_games)} games · IWD ${r.iwd == null ? "n/a" : (r.iwd >= 0 ? "+" : "") + (100 * r.iwd).toFixed(1) + "pp"}`);
    else out.push(`GIH WR n/a: ${fmtInt(r.gih_games)} games, 17Lands needs ${fmtInt(need)}`);
    out.push(`ALSA ${fmt1(r.alsa)} · ATA ${fmt1(r.ata)} · seen ${fmtInt(r.seen_count)}×`);
    if (r.gp_wr != null) out.push(`GP WR ${fmtPct(r.gp_wr)} · OH WR ${fmtPct(r.oh_wr)} · ${fmtInt(r.games)} games`);
    if (r.play_rate != null) out.push(`played in ${fmtPct(r.play_rate, 0)} of decks that had it`);
    out.push(`17Lands ${lastMeta ? lastMeta.expansion : ""} · ${setLabel()}`);
    return out;
  }
  function ratingBadge(name) {
    const r = ratings[name]; if (!r || !prefs.showRatings) return null;
    const hasWr = r.gih_wr != null;
    if (!hasWr && r.alsa == null) return null;
    const pct = hasWr ? (r.gih_pct == null ? 50 : r.gih_pct) : (r.alsa_pct == null ? 50 : r.alsa_pct);
    const b = el("div", `rate ${pct >= 85 ? "r-hot" : pct >= 55 ? "r-mid" : "r-low"}`);
    if (hasWr) b.appendChild(el("span", "wr", fmtPct(r.gih_wr)));
    if (r.alsa != null) b.appendChild(el("span", "alsa", fmt1(r.alsa)));
    return b;
  }

  // ------------------------------------------------------------ card cells
  // size: "lg" for the pack on screen, "" (66px) elsewhere
  function tile(name, size, extra) {
    const i = info(name), hasImg = !!(i && i.image);
    const t = el("div", `tile ${size || ""} ${hasImg ? "has-img" : "g-" + group(name)}${extra ? " " + extra : ""}`);
    t.dataset.card = name;
    const showName = () => {
      if (t.querySelector(".name")) return;
      t.appendChild(el("div", "name", name));
      if (i && (i.mana_cost || i.group === "L")) t.appendChild(el("div", "meta", mana(i)));
    };
    if (hasImg) {
      const img = el("img"); img.src = i.image; img.alt = name; img.loading = "lazy";
      img.onerror = () => { img.remove(); t.classList.remove("has-img"); t.classList.add("g-" + group(name)); showName(); };
      t.appendChild(img);
    } else showName();
    const b = ratingBadge(name); if (b) t.appendChild(b);
    return t;
  }
  function chip(name, cls, text) {
    const c = el("span", `chip g-${group(name)}${cls ? " " + cls : ""}`, text || name);
    c.dataset.card = name;
    return c;
  }

  // ------------------------------------------------------------ header
  function renderHeader(st) {
    const d = st.draft, p = st.position;
    $("position").textContent = p.status === "idle" ? "No draft" : `Pack ${p.pack} · Pick ${p.pick}`;
    const pill = $("state");
    pill.className = "pill " + p.status;
    const src = st.sources || {}, srcProblem = (src.mtgo && src.mtgo.watched && src.mtgo.status !== "ok") || (src.arena && src.arena.watched && src.arena.status !== "ok");
    pill.textContent = p.status === "on_screen" ? "pack on screen" : p.status === "waiting" ? "waiting for next pack"
      : (st.file ? "no picks yet" : srcProblem ? "no draft log · check settings ⚙" : "no draft log");
    const podNote = { inferred: " (inferred from wheels)", assumed: " (assumed)" }[d ? d.pod_size_source : ""] || "";
    $("cube").textContent = d ? `${d.source === "arena" ? "Arena" : "MTGO"} · ${d.set_name || "unknown cube"} · ${d.pod_size || "?"} players${podNote}${d.cards_per_pick > 1 ? ` · ${d.cards_per_pick} cards per pick` : ""}` : "";
    const pend = $("cards-pending");
    pend.textContent = st.cards_pending ? `fetching ${st.cards_pending} cards…` : (st.card_errors && st.card_errors.length ? `${st.card_errors.length} card errors` : "");
    pend.title = (st.card_errors || []).join("\n");
    const w = $("warnings"); w.hidden = !(st.warnings || []).length; w.textContent = (st.warnings || []).join("\n");
    $("nav-deck").hidden = !(st.arena_game && (st.arena_game.deck || st.arena_game.game));
    let file;
    if (st.source === "arena") file = st.file ? `Arena · ${st.log_dir}\\${st.file}` : `Arena · watching ${st.log_dir} — no draft yet`;
    else if (st.source === "auto") file = `watching MTGO ${st.log_dir || "(no folder set)"} and Arena ${st.arena_log} — no draft yet`;
    else if (!st.log_dir) file = "no draft log folder set";
    else if (!st.log_dir_ok) file = `${st.log_dir} — folder not found`;
    else file = st.file ? `${st.log_dir}\\${st.file}` : `watching ${st.log_dir} — no .txt logs yet`;
    $("file").textContent = file; $("file-short").textContent = file;
    const upd = st.updated_at ? `updated ${st.updated_at.replace("T", " ")}` : "";
    $("updated").textContent = upd; $("updated-2").textContent = upd;
  }

  // ------------------------------------------------------------ settings
  const settings = $("settings"), gear = $("gear");
  gear.addEventListener("click", () => { settings.hidden = !settings.hidden; gear.classList.toggle("on", !settings.hidden); });
  document.addEventListener("click", (ev) => { if (!settings.hidden && !settings.contains(ev.target) && ev.target !== gear) { settings.hidden = true; gear.classList.remove("on"); } });
  function bindOpt(id, key) {
    const cb = $(id); cb.checked = prefs[key];
    cb.addEventListener("change", () => { prefs[key] = cb.checked; savePrefs(); render(lastState); });
  }
  bindOpt("opt-badges", "showRatings"); bindOpt("opt-top", "topPlayers"); bindOpt("opt-latest", "latestEvent");
  bindOpt("opt-multi", "tallyMulti"); bindOpt("opt-lands", "tallyLands");

  function renderRatings(st) {
    const meta = st.ratings_meta, bar = $("ratings-bar"), short = $("ratings-short");
    clear(bar); short.textContent = "";
    if (!meta) { bar.textContent = "17Lands: no data"; return; }
    const key = wantedKey(), shown = key ? meta.sets[key] : meta;
    const seen = Object.keys(st.ratings || {}).length + (st.ratings_unmatched || []).length;
    const miss = st.ratings_unmatched || [];
    if (meta.status === "ok") {
      const nextIn = shown.next_check_hours;
      const next = nextIn == null ? "" : (nextIn < 0.1 ? " · checking soon" : ` · next check in ${nextIn < 1 ? Math.round(nextIn * 60) + "m" : Math.round(nextIn) + "h"}`);
      const when = shown.age_hours == null ? "" : (shown.age_hours < 1 ? "fetched just now" : `fetched ${Math.round(shown.age_hours)}h ago`) + next;
      bar.appendChild(el("div", null, `17Lands · ${meta.expansion} · ${meta.format} · ${setLabel()}`));
      bar.appendChild(el("div", null, `${fmtInt(shown.cards)} cards · ${fmtInt(shown.with_win_rate)} with a win rate (≥${fmtInt(meta.min_games)} games) · ${when}${meta.fetching ? " · refreshing…" : ""}`));
      short.textContent = `17Lands · ${meta.expansion} · ${setLabel()}`;
    } else if (meta.status === "fetching") bar.appendChild(el("div", null, "17Lands: downloading…"));
    else if (meta.status === "error") bar.appendChild(el("div", "err", `17Lands unavailable: ${meta.error}`));
    else bar.appendChild(el("div", null, "17Lands: no data yet"));
    if (seen) {
      const u = el("div", miss.length ? "unmatched" : null, miss.length ? `${miss.length} of ${seen} cards seen have no Arena data` : `all ${seen} cards seen have data`);
      u.title = miss.length ? "No 17Lands entry for:\n" + miss.join("\n") : "";
      bar.appendChild(u);
      if (miss.length) { const s = el("span", "unmatched", ` · ${miss.length} of ${seen} seen have no data`); s.title = u.title; short.appendChild(s); }
    }
    // the dataset toggles are enabled once their pull has loaded
    const sets = meta.sets || {};
    const ready = (period, grp) => (period === meta.time_period && !grp) || !!(sets[`${period}|${grp}`] && sets[`${period}|${grp}`].status === "ok");
    const why = (period, grp) => { const s = sets[`${period}|${grp}`]; if (!s) return "not enabled"; if (s.fetching || s.status === "fetching") return "loading…"; if (s.status === "error") return `failed, retry in ${s.retry_in == null ? "?" : s.retry_in + "s"}`; return s.status; };
    const periodNow = prefs.latestEvent ? "LATEST_EVENT" : meta.time_period, groupNow = prefs.topPlayers ? "top" : "";
    const setTog = (wrapId, cbId, stId, ok, state) => { $(wrapId).classList.toggle("off", !ok); $(cbId).disabled = !ok; $(stId).textContent = ok ? "" : state; };
    setTog("opt-top-wrap", "opt-top", "opt-top-state", ready(periodNow, "top"), why(periodNow, "top"));
    setTog("opt-latest-wrap", "opt-latest", "opt-latest-state", ready("LATEST_EVENT", groupNow), why("LATEST_EVENT", groupNow));
  }

  // ------------------------------------------------------------ log locations
  // Both clients' logs are set here. Each shows a status pill and, when the
  // log can't be used, the steps that fix it.
  const form = $("logdir-form"), input = $("logdir-input"), msg = $("logdir-msg");
  const aform = $("arena-form"), ainput = $("arena-input"), amsg = $("arena-msg");
  let problemPrompted = false;     // open the settings for a problem once, not on every update
  let arenaDefault = "";
  function helpList(items) {
    const ol = el("ol"); for (const t of items) ol.appendChild(el("li", null, t)); return ol;
  }
  function setStatus(id, cls, text) { const s = $(id); s.className = "src-status " + cls; s.textContent = text; }
  function updateLogDir(st) {
    $("logdir-grp").hidden = !!st.config_locked; $("arena-grp").hidden = !!st.config_locked;
    const src = st.sources || {}, m = src.mtgo || {}, a = src.arena || {};
    if (document.activeElement !== input) input.value = m.log_dir || st.log_dir || "";
    arenaDefault = a.default_path || "";
    if (document.activeElement !== ainput) ainput.value = a.path || "";
    $("arena-reset").hidden = !!a.is_default;

    // MTGO
    const mh = $("mtgo-help"); clear(mh);
    const mtgoSteps = ["In MTGO open Settings and tick Save Draft Log.", "Set the folder it writes to (any folder you like).", "Paste that folder above and press Save."];
    if (!m.watched) setStatus("mtgo-status", "off", "not watched in this run");
    else if (m.status === "ok") setStatus("mtgo-status", "ok", `found · ${m.logs_found} draft log${m.logs_found === 1 ? "" : "s"}`);
    else if (m.status === "no_logs") { setStatus("mtgo-status", "warn", "folder found, no draft logs in it yet"); mh.appendChild(document.createTextNode("Nothing has been written there yet. If you have drafted since turning the setting on, check the folder MTGO shows next to Save Draft Log is this one.")); }
    else if (m.status === "missing") { setStatus("mtgo-status", "bad", "folder not found"); mh.appendChild(document.createTextNode("That folder does not exist on this machine.")); mh.appendChild(helpList(mtgoSteps)); }
    else { setStatus("mtgo-status", "bad", "not set"); mh.appendChild(document.createTextNode("The tool does not know where MTGO writes its draft logs.")); mh.appendChild(helpList(mtgoSteps)); }

    // Arena
    const ah = $("arena-help"); clear(ah);
    const arenaSteps = ["Open Arena and go to Options → Account.", "Turn on Detailed Logs (Plugin Support).", "Restart Arena; the log is written from then on."];
    if (!a.watched) setStatus("arena-status", "off", "not watched in this run");
    else if (a.status === "ok") setStatus("arena-status", "ok", a.detailed_logs === true ? "found · detailed logs on" : "found");
    else if (a.status === "detailed_logs_off") { setStatus("arena-status", "bad", "detailed logs are off"); ah.appendChild(document.createTextNode("The log is there, but Arena wrote it without draft or game details, so nothing can be read from it.")); ah.appendChild(helpList(arenaSteps)); }
    else { setStatus("arena-status", "bad", "log not found"); ah.appendChild(document.createTextNode(`No Player.log at ${a.path || "the path above"}. ${a.is_default ? "That is where every normal Arena install writes it, so Arena has probably not run with detailed logs on yet." : "You have set a custom path; make sure it is right, or press Use default."}`)); ah.appendChild(helpList(arenaSteps)); }

    // flag a problem once: gear turns gold and the panel opens, but only while nothing is on screen
    const problem = (m.watched && m.status !== "ok") || (a.watched && a.status !== "ok");
    gear.classList.toggle("alert", problem);
    if (problem && !st.picks.length && !problemPrompted) { problemPrompted = true; settings.hidden = false; gear.classList.add("on"); }
    if (!problem) problemPrompted = false;
  }
  async function postConfig(payload, target) {
    target.className = "logdir-msg"; target.textContent = "saving…";
    try {
      const r = await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const res = await r.json();
      if (!res.ok) { target.className = "logdir-msg err"; target.textContent = res.error || "could not save"; return null; }
      target.className = "logdir-msg " + (res.warning ? "err" : "ok");
      return res;
    } catch (e) { target.className = "logdir-msg err"; target.textContent = "server not reachable"; return null; }
  }
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const res = await postConfig({ log_dir: input.value }, msg);
    if (res) msg.textContent = `${res.watched ? "watching" : "saved"} ${res.log_dir} · ${res.logs_found} log${res.logs_found === 1 ? "" : "s"} found${res.newest ? ` · newest: ${res.newest}` : ""}${res.warning ? ` · ${res.warning}` : ""}`;
  });
  aform.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const value = ainput.value.trim();
    if (value && arenaDefault && value.replace(/\//g, "\\").toLowerCase() !== arenaDefault.replace(/\//g, "\\").toLowerCase()) {
      if (!window.confirm("The default location is right for a normal Arena install. Only change it if you are sure Arena writes Player.log somewhere else on this machine.\n\nUse this custom path?")) return;
    }
    const res = await postConfig({ arena_log: value }, amsg);
    if (res) amsg.textContent = `${res.watched ? "watching" : "saved"} ${res.arena_log}${res.is_default ? " (default)" : ""}${res.warning ? ` · ${res.warning}` : res.exists ? " · found" : ""}`;
  });
  $("arena-reset").addEventListener("click", async () => {
    const res = await postConfig({ arena_log: "" }, amsg);
    if (res) amsg.textContent = `back to the default: ${res.arena_log}${res.warning ? ` · ${res.warning}` : res.exists ? " · found" : ""}`;
  });

  // ------------------------------------------------------------ current pack
  function renderCurrent(st) {
    const list = $("current-cards"), note = $("current-wheel"), empty = $("current-empty");
    clear(list); clear(note); note.hidden = true;
    const cur = st.current_pack;
    if (!cur) {
      $("current-count").textContent = "";
      empty.textContent = st.picks.length ? "Waiting for the next pack…" : (st.file ? "Draft log found; waiting for the first pack." : "Start a draft and the pack will appear here.");
      return;
    }
    empty.textContent = "";
    $("current-count").textContent = `${cur.cards.length} cards, in client order · ${pos(cur.pack, cur.pick)}`;
    for (const c of cur.cards) list.appendChild(tile(c, "lg"));
    const w = st.wheels.find((x) => x.pack === cur.pack && x.return_pick === cur.pick);
    if (w) {
      note.hidden = false;
      note.appendChild(document.createTextNode("Your "));
      note.appendChild(el("span", "mono", pos(w.pack, w.first_pick)));
      note.appendChild(document.createTextNode(" pack, back around. You took "));
      note.appendChild(el("b", null, w.your_pick));
      note.appendChild(document.createTextNode(`; the pod took `));
      const chips = el("span", "chips");
      for (const n of w.taken) chips.appendChild(chip(n));
      note.appendChild(chips);
    }
  }

  // ------------------------------------------------------------ pod circle
  // You at the bottom; the seats are fixed physical positions numbered
  // clockwise from you (seat 2 is on your left), the same all draft long.
  // Packs 1 and 3 pass left, so a pack reaches you from your right; pack 2
  // passes right. Only the packs move between rounds, not the people.
  function renderPod(st) {
    const box = $("pod"); clear(box);
    const d = st.draft, p = st.position;
    $("pane-pod").hidden = !d || p.status === "idle";
    if ($("pane-pod").hidden) return;
    const n = d.pod_size || 8, cpp = d.cards_per_pick || 1, size = d.pack_sizes[String(p.pack)] || 15, lastPick = Math.ceil(size / cpp);
    const passLeft = p.pack !== 2;
    // the arrival order in words, so it never depends on reading the circle
    const order = st.in_flight.slice().sort((a, b) => a.due_pick - b.due_pick)
      .map((f) => `${pos(f.pack, f.first_pick)} at pick ${f.due_pick} (${Math.max(0, f.passed.length - (n - 1) * cpp)} left)`);
    $("pod-note").textContent = `${passLeft ? "packs pass left: yours come back from your right" : "packs pass right: yours come back from your left"} · hover a seat to see its pack`
      + (order.length ? ` · coming back to you: ${order.join(", ")}` : "");
    box.appendChild(el("div", "ring"));
    const c = el("div", "centre", passLeft ? "↻" : "↺"); c.appendChild(el("small", null, passLeft ? "packs pass left" : "packs pass right")); box.appendChild(c);
    const heroIdx = d.players.indexOf(d.hero);
    const byU = new Map(st.in_flight.map((f) => [f.picks_until_return, f]));
    for (let s = 0; s < n; s++) {
      // physical seat s, going clockwise from you (left on screen first)
      const ang = Math.PI / 2 + s * 2 * Math.PI / n;
      // the pack at this seat reaches you in u picks: counting against the passing direction
      const u = passLeft ? (n - s) % n : s;
      const seat = el("div", "seat" + (s === 0 ? " hero" : ""));
      seat.style.left = (210 + 150 * Math.cos(ang)) + "px"; seat.style.top = (170 + 125 * Math.sin(ang)) + "px";
      const pi = heroIdx >= 0 ? (heroIdx + s) % n : -1;
      seat.appendChild(el("div", "who", s === 0 ? "you" : (pi >= 0 && d.players[pi]) || `seat ${s + 1}`));
      const pack = el("div", "pack"); pack.dataset.seat = String(u);
      const f = byU.get(u);
      if (u === 0) {
        // between picks, a pack due at the very next pick is on its way to you
        if (st.current_pack) { pack.appendChild(el("b", null, `${st.current_pack.cards.length} cards`)); pack.appendChild(el("span", null, "in front of you")); }
        else if (f) { pack.appendChild(el("b", null, pos(f.pack, f.first_pick))); pack.appendChild(el("span", null, "arriving now")); }
        else { pack.appendChild(el("b", null, "—")); pack.appendChild(el("span", null, "waiting")); }
      }
      else if (f) {
        const left = Math.max(0, f.passed.length - (n - 1) * cpp);
        pack.appendChild(el("b", null, pos(f.pack, f.first_pick)));
        pack.appendChild(el("span", null, `${left} of ${f.passed.length} left at pick ${f.due_pick}`));
      }
      else { seat.classList.add("unseen"); pack.appendChild(el("b", null, "unseen")); pack.appendChild(el("span", null, p.pick + u > lastPick ? "won't reach you" : `due pick ${p.pick + u}`)); }
      seat.appendChild(pack); box.appendChild(seat);
    }
  }

  // ------------------------------------------------------------ picks
  $("picks-toggle").addEventListener("click", () => { prefs.picksOpen = !prefs.picksOpen; savePrefs(); renderPicks(lastState); });
  function renderPicks(st) {
    if (!st) return;
    const box = $("picks"); clear(box);
    const done = st.picks.filter((p) => p.complete);
    const n = done.reduce((s, p) => s + (p.picked_all || [p.picked]).length, 0);
    $("pick-count").textContent = n ? `${n} so far` : "";
    $("picks-hint").textContent = prefs.picksOpen ? "collapse" : "expand";
    $("pane-picks").classList.toggle("open", prefs.picksOpen);
    box.hidden = !prefs.picksOpen;
    const flashNew = lastCommitted >= 0 && done.length > lastCommitted; lastCommitted = done.length;
    const newest = done.length ? done[done.length - 1].picked : null;
    const byPack = new Map();
    for (const p of done) { if (!byPack.has(p.pack)) byPack.set(p.pack, []); byPack.get(p.pack).push(p); }
    for (const [pack, picks] of byPack) {
      const row = el("div", "row"), lbl = el("div", "lbl");
      lbl.appendChild(el("span", "big", `Pack ${pack}`)); lbl.appendChild(el("span", "sub", `${picks.length} picks, in order`)); row.appendChild(lbl);
      const tiles = el("div", "tiles tight");
      for (const p of picks) for (const name of (p.picked_all || [p.picked])) {
        const t = tile(name, "", flashNew && p.picked === newest ? "flash" : ""); t.appendChild(el("span", "pickno", String(p.pick))); tiles.appendChild(t);
      }
      row.appendChild(tiles); box.appendChild(row);
    }
  }

  // ------------------------------------------------------------ wheels
  $("older-toggle").addEventListener("click", () => { prefs.olderOpen = !prefs.olderOpen; savePrefs(); renderWheels(lastState); });
  function colorBuckets(name) {
    const i = info(name); if (!i || i.status !== "ok") return ["?"];
    if (i.group === "L") { if (!prefs.tallyLands) return []; return i.color_identity.length ? i.color_identity : ["C"]; }
    if (i.group === "M") return prefs.tallyMulti ? i.colors : [];
    return i.colors.length ? i.colors : ["C"];
  }
  function renderWheels(st) {
    if (!st) return;
    const box = $("wheels"), p = st.position; clear(box);
    const all = st.wheels.slice().reverse(), round = all.filter((w) => w.pack === p.pack), older = all.length - round.length;
    const shown = prefs.olderOpen ? all : round;
    $("wheels-empty").hidden = !!all.length;
    const ot = $("older-toggle"); ot.hidden = !older; ot.textContent = prefs.olderOpen ? "hide earlier packs" : `show earlier packs (${older})`;
    // the tally: of what you passed in wheeled packs, how much of each colour the pod took
    const tally = $("taken"); clear(tally);
    const taken = {}, passed = {}; let total = 0;
    for (const w of st.wheels) { const gone = new Set(w.taken); for (const n of w.passed) { const g = gone.has(n); if (g) total++; for (const c of colorBuckets(n)) { passed[c] = (passed[c] || 0) + 1; if (g) taken[c] = (taken[c] || 0) + 1; } } }
    if (total) {
      const parts = ["W", "U", "B", "R", "G", "C"].filter((c) => passed[c]).map((c) => `${c} ${taken[c] || 0}/${passed[c]}`);
      tally.textContent = `Pod took ${total} of what you passed · ${parts.join(" · ")} (taken / passed)`;
    }
    for (const w of shown) {
      const row = el("div", "row"), lbl = el("div", "lbl");
      lbl.appendChild(el("span", "big", `${pos(w.pack, w.first_pick)} → ${pos(w.pack, w.return_pick)}`));
      const sub = el("span", "sub"); sub.appendChild(el("b", null, String(w.returned.length))); sub.appendChild(document.createTextNode(" came back")); lbl.appendChild(sub);
      // what you took from this pack: the first time, and (if already picked) when it came back
      const second = st.picks.find((x) => x.pack === w.pack && x.pick === w.return_pick && x.complete);
      const you = el("span", "you"); you.appendChild(document.createTextNode("you took "));
      you.appendChild(el("span", null, w.your_pick));
      if (second) { you.appendChild(document.createTextNode(", then ")); you.appendChild(el("span", null, (second.picked_all || [second.picked]).join(" + "))); }
      lbl.appendChild(you); row.appendChild(lbl);
      const split = el("div", "split"), left = el("div", "left"), right = el("div", "right");
      const back = new Set(w.returned);
      for (const n of w.passed) if (back.has(n)) left.appendChild(tile(n));
      for (const n of w.passed) if (!back.has(n)) right.appendChild(chip(n));
      split.appendChild(left); split.appendChild(el("div", "bar")); split.appendChild(right); row.appendChild(split);
      if (w.warning) row.appendChild(el("div", "hint", w.warning));
      box.appendChild(row);
    }
  }

  // ------------------------------------------------------------ find a card
  let poolQuery = "";
  $("pool-search").addEventListener("input", (ev) => { poolQuery = ev.target.value.trim().toLowerCase(); if (lastState) renderPool(lastState); });
  function poolStatus(c) {
    switch (c.state) {
      case "mine": return ["mine", `you picked it ${pos(c.pack, c.pick)}`];
      case "on_screen": return ["screen", "on screen now"];
      case "in_flight": return ["flight", `seen ${pos(c.pack, c.pick)} · passed, due back at pick ${c.due}`];
      case "gone": return ["gone", `seen ${pos(c.pack, c.pick)} · ${{ taken: "the pod took it", no_wheel: "its pack will not come round again", pack_over: "that booster is over", unknown_pod: "passed" }[c.reason] || "gone"}`];
      default: return ["unseen", "not seen yet"];
    }
  }
  function renderPool(st) {
    const p = st.pool, box = $("pool"); clear(box);
    $("pane-pool").hidden = !p || !st.picks.length;
    if ($("pane-pool").hidden) return;
    const seen = p.cards.length - (p.counts.unseen || 0);
    $("pool-summary").textContent = p.cube ? `${seen} seen · ${p.counts.unseen} not seen yet (${p.cube.name} list)` : `${seen} cards seen · no cube list, so unseen cards cannot be searched`;
    if (poolQuery.length < 2) return;
    const hits = p.cards.filter((c) => c.name.toLowerCase().includes(poolQuery)).slice(0, 25);
    if (!hits.length) { box.appendChild(el("p", "empty", p.cube ? `“${poolQuery}”: not in this draft or the cube list` : `“${poolQuery}”: not seen so far`)); return; }
    for (const c of hits) {
      const [cls, text] = poolStatus(c), row = el("div", "find-row"); row.dataset.card = c.name;
      row.appendChild(el("span", "find-name", c.name)); row.appendChild(el("span", `find-status s-${cls}`, text)); box.appendChild(row);
    }
  }

  // ------------------------------------------------------------ deck / game view
  $("tab-deck").addEventListener("click", () => { prefs.deckMode = "deck"; savePrefs(); renderDeck(lastState); });
  $("tab-lib").addEventListener("click", () => { prefs.deckMode = "lib"; savePrefs(); renderDeck(lastState); });
  const sum = (list) => list.reduce((s, c) => s + c.n, 0);
  // A card in the curve. With an image, nothing is drawn over it: the stack
  // shows each card's own printed title band, and duplicates are stacked as
  // separate copies. Without an image, a text cell carries name, cost and count.
  function cardFace(name, count, zone, showMana) {
    const i = info(name), hasImg = !!(i && i.image);
    const c = el("div", "card" + (hasImg ? " has-img" : "")); c.dataset.card = name;
    const face = el("div", "face" + (hasImg ? "" : " g-" + group(name)));
    if (hasImg) { const img = el("img"); img.src = i.image; img.alt = name; img.loading = "lazy"; face.appendChild(img); c.appendChild(face); return c; }
    const bar = el("div", "bar"); bar.appendChild(el("span", "n", name));
    if (zone) bar.appendChild(el("span", "z", zone));
    if (showMana && i && i.group !== "L" && i.mana_cost) bar.appendChild(el("span", "m", mana(i)));
    if (count > 1) bar.appendChild(el("span", "x", `×${count}`));
    face.appendChild(bar);
    face.appendChild(el("div", "body", i ? i.type_line : ""));
    c.appendChild(face);
    return c;
  }
  function renderDeck(st) {
    if (!st) return;
    const ag = st.arena_game || {}, dk = ag.deck, g = ag.game, lib = prefs.deckMode === "lib";
    $("tab-deck").classList.toggle("on", !lib); $("tab-lib").classList.toggle("on", lib);
    const curve = $("deck-curve"), side = $("deck-side"), unexp = $("deck-unexpected"), empty = $("deck-empty");
    clear(curve); clear(side); clear(unexp); unexp.hidden = true; empty.textContent = "";
    // status line doubles as the game header when a game is on
    if (g) {
      $("position").textContent = g.over ? `Game ${g.game_number || "?"} over` : `Game ${g.game_number || "?"}${g.turn ? " · turn " + g.turn : ""}`;
      const pill = $("state");
      if (g.wins != null && g.losses != null) { const w = g.wins, l = g.losses; pill.className = "pill " + (w > l ? "up" : w < l ? "down" : "waiting"); pill.textContent = w === l ? `tied ${w}–${l}` : w > l ? `up ${w}–${l}` : `down ${w}–${l}`; }
      else { pill.className = "pill"; pill.textContent = g.stage || "in progress"; }
      const me = g.my_seat, lifeMe = me != null ? g.life[String(me)] : undefined;
      const lifeOpp = Object.entries(g.life || {}).filter(([s]) => String(s) !== String(me)).map(([, v]) => v)[0];
      $("cube").textContent = `${g.opponent ? "vs " + g.opponent : ""}${lifeMe !== undefined ? ` · life ${lifeMe} – ${lifeOpp === undefined ? "?" : lifeOpp}` : ""}${g.library != null ? ` · ${g.library} in library` : ""}${g.seat_known ? "" : " · seat not known yet"}`;
    }
    if (!dk) { empty.textContent = g ? "No deck submission in this log, so the library cannot be worked out." : "No Arena deck or game in this log yet."; $("pane-opp").hidden = !g; }
    else {
      const total = dk.main_count || sum(dk.main);
      // what has left the library: deck minus remaining (when the server knows), else seen_mine
      const left = new Map();
      if (lib && g) {
        if (g.remaining) { const rem = new Map(g.remaining.map((c) => [c.name, c.n])); for (const c of dk.main) left.set(c.name, c.n - (rem.get(c.name) || 0)); }
        else for (const c of (g.seen_mine || [])) left.set(c.name, c.n);
      }
      const inLib = dk.main.reduce((s, c) => s + Math.max(0, c.n - (left.get(c.name) || 0)), 0);
      $("deck-note").textContent = lib ? `${inLib} of ${total} still in your library · cards below the dashed line have left it` : `${total} cards, as submitted`;
      const cols = new Map();
      for (const c of dk.main) { const i = info(c.name), k = i && i.group === "L" ? "L" : Math.min(7, Math.round(i ? i.cmc : 0)); if (!cols.has(k)) cols.set(k, []); cols.get(k).push(c); }
      const keys = [...cols.keys()].sort((a, b) => (a === "L") - (b === "L") || a - b);
      for (const k of keys) {
        const col = el("div", "col"); col.appendChild(el("div", "cv", k === "L" ? "lands" : k === 7 ? "7+" : String(k)));
        const stack = el("div", "stack"), gone = el("div", "gone-list");
        for (const c of cols.get(k).slice().sort((a, b) => a.name.localeCompare(b.name))) {
          const out = left.get(c.name) || 0, remain = c.n - out;
          if (lib && remain <= 0) { const gc = el("div", "gone"); gc.dataset.card = c.name; gc.appendChild(el("span", "n", c.name)); gc.appendChild(el("span", "z", out > 1 ? `${out} out` : "out")); gone.appendChild(gc); continue; }
          const shown = lib ? remain : c.n, i = info(c.name);
          if (i && i.image) { for (let k2 = 0; k2 < shown; k2++) stack.appendChild(cardFace(c.name, 1, null, false)); }
          else stack.appendChild(cardFace(c.name, shown, lib && out ? `${out} out` : null, !(lib && out)));
          if (lib && out && i && i.image) { const gc = el("div", "gone"); gc.dataset.card = c.name; gc.appendChild(el("span", "n", c.name)); gc.appendChild(el("span", "z", `${out} of ${c.n} out`)); gone.appendChild(gc); }
        }
        col.appendChild(stack); if (gone.childNodes.length) col.appendChild(gone); curve.appendChild(col);
      }
      side.appendChild(el("span", "lbl", `Sideboard · ${dk.side_count || sum(dk.side)}`));
      for (const c of dk.side) side.appendChild(chip(c.name, "side", c.n > 1 ? `${c.n}× ${c.name}` : c.name));
      if (lib && g && g.unexpected && g.unexpected.length) {
        unexp.hidden = false; unexp.appendChild(el("span", null, "Seen this game but not in the submitted main deck:"));
        for (const c of g.unexpected) unexp.appendChild(chip(c.name, "warn", c.n > 1 ? `${c.n}× ${c.name}` : c.name));
        unexp.appendChild(el("span", "muted", "sideboarded in, or the deck changed"));
      }
      $("pane-opp").hidden = !g;
    }
    if (g) {
      $("opp-title").textContent = `${g.opponent || "Opponent"} has shown`;
      const theirs = g.seen_theirs || [], colors = [...new Set(theirs.flatMap((c) => (info(c.name) || {}).color_identity || []))];
      $("opp-note").textContent = `${sum(theirs)} cards this match${colors.length ? " · colours " + colors.join(" ") : ""}`;
      const box = $("opp-cards"); clear(box);
      for (const c of theirs) { const t = tile(c.name, "lg"); if (c.n > 1) t.appendChild(el("span", "pickno", `×${c.n}`)); box.appendChild(t); }
    }
    $("deck-foot").textContent = dk ? `Arena · deck submitted ${(dk.submitted_at || "").replace("T", " ")}${dk.event ? " · " + dk.event : ""}` : "";
  }

  // ------------------------------------------------------------ routing
  function route() {
    const deck = location.hash === "#deck";
    $("view-deck").hidden = !deck; $("view-draft").hidden = deck;
    $("nav-draft").hidden = !deck;
    if (lastState) render(lastState);
  }
  window.addEventListener("hashchange", route);

  // ------------------------------------------------------------ render
  let uiVersion = null;
  function render(st) {
    if (!st) return;
    if (st.ui_version) { if (uiVersion === null) uiVersion = st.ui_version; else if (st.ui_version !== uiVersion) { location.reload(); return; } }
    lastState = st; cards = st.cards || {}; lastMeta = st.ratings_meta;
    const key = wantedKey(); ratings = (key ? (st.ratings_sets || {})[key] : st.ratings) || {};
    // the deck view only makes sense while there is an Arena deck or game; an MTGO
    // draft (or a fresh log) must never be hidden behind a stale #deck address
    const hasDeck = !!(st.arena_game && (st.arena_game.deck || st.arena_game.game));
    if (location.hash === "#deck" && !hasDeck) { history.replaceState(null, "", location.pathname); route(); return; }
    renderHeader(st); renderRatings(st); updateLogDir(st);
    if (location.hash === "#deck") { renderDeck(st); return; }
    renderCurrent(st); renderPod(st); renderPicks(st); renderWheels(st); renderPool(st);
  }

  // ------------------------------------------------------------ hover popover
  const preview = $("preview");
  function placePreview(ev) {
    const pad = 16, w = preview.offsetWidth || 420, h = preview.offsetHeight || 380;
    let x = ev.clientX + pad, y = ev.clientY + pad;
    if (x + w > window.innerWidth) x = Math.max(0, ev.clientX - w - pad);
    if (y + h > window.innerHeight) y = Math.max(0, window.innerHeight - h - pad);
    preview.style.left = x + "px"; preview.style.top = y + "px";
  }
  function showCard(name) {
    clear(preview); preview.className = "popover";
    const i = info(name), big = el("div", "big");
    if (i && i.image) { const img = el("img"); img.src = i.image; img.alt = name; big.appendChild(img); }
    else { const t = el("div", `tile g-${group(name)}`); t.appendChild(el("div", "name", name)); if (i) t.appendChild(el("div", "meta", mana(i))); big.appendChild(t); }
    preview.appendChild(big);
    const inf = el("div", "info"); inf.appendChild(el("div", "t", name));
    if (i && i.type_line) inf.appendChild(el("div", "ty", i.type_line + (i.mana_cost ? "  " + i.mana_cost : "")));
    const lines = ratingLines(name); if (!lines.length) lines.push("no 17Lands data");
    for (const l of lines) inf.appendChild(el("div", "l", l));
    preview.appendChild(inf); preview.hidden = false;
  }
  function showPack(u) {
    const st = lastState; if (!st) return;
    const f = st.in_flight.find((x) => x.picks_until_return === u);
    let names = u === 0 && st.current_pack ? st.current_pack.cards : f ? f.passed : null;
    if (!names) { preview.hidden = true; return; }
    clear(preview); preview.className = "popover pack";
    if (u === 0 && st.current_pack) {
      preview.appendChild(el("div", "title", `In front of you · ${names.length} cards`));
    } else {
      // what you passed: the pod takes (N-1) per lap before it returns, so only
      // a few of these come back. Most likely to wheel first (highest ALSA).
      const d = st.draft, n = d.pod_size || 8, cpp = d.cards_per_pick || 1;
      const gone = Math.min(names.length, (n - 1) * cpp), left = names.length - gone;
      preview.appendChild(el("div", "title", `${pos(f.pack, f.first_pick)} · you took ${f.your_pick} and passed ${names.length}`));
      preview.appendChild(el("div", "title warn", `the pod takes ${gone} of these before it returns at pick ${f.due_pick}: only ${left} will be there`));
      preview.appendChild(el("div", "title muted", "sorted by how likely each is to wheel (17Lands ALSA, highest first)"));
      const alsa = (nm) => { const r = ratings[nm]; return r && r.alsa != null ? r.alsa : -1; };
      names = names.slice().sort((a, b) => alsa(b) - alsa(a));
    }
    const tiles = el("div", "tiles"); for (const n of names) tiles.appendChild(tile(n)); preview.appendChild(tiles); preview.hidden = false;
  }
  document.addEventListener("mouseover", (ev) => {
    const seat = ev.target.closest("[data-seat]");
    if (seat) { showPack(Number(seat.dataset.seat)); placePreview(ev); return; }
    const t = ev.target.closest("[data-card]");
    if (!t) { preview.hidden = true; return; }
    showCard(t.dataset.card); placePreview(ev);
  });
  document.addEventListener("mousemove", (ev) => { if (!preview.hidden) placePreview(ev); });
  document.addEventListener("mouseout", (ev) => { if (ev.target.closest && ev.target.closest("[data-card],[data-seat]")) preview.hidden = true; });

  // ------------------------------------------------------------ transport
  function setConn(state, text) { $("conn-dot").className = "dot " + state; $("conn-text").textContent = text; }
  route();
  if (DEMO) {
    fetch("../data/sample-state.json").then((r) => r.json()).then((st) => { setConn("live", "demo data"); render(st); });
  } else {
    fetch("/api/state").then((r) => r.json()).then(render).catch(() => {});
    const es = new EventSource("/events");
    es.addEventListener("state", (ev) => { setConn("live", "live"); try { render(JSON.parse(ev.data)); } catch (e) { console.error(e); } });
    es.onopen = () => setConn("live", "live");
    es.onerror = () => setConn("down", "reconnecting…");
  }
})();
