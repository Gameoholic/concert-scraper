// Live scan progress: renders the [data-progress] block from /scan/progress and keeps it current.
// Polls every 1.5 s while a scan runs (5 s when the tab is hidden), fetching only what changed.
(() => {
  const root = document.querySelector("[data-progress]");
  if (!root) return;
  const $ = (sel) => root.querySelector(sel);

  const GROUPS = [
    { key: "x", title: "X accounts", icon: "𝕏" },
    { key: "instagram", title: "Instagram", icon: "IG" },
    { key: "website", title: "Websites", icon: "WEB" },
    { key: "claude", title: "Claude reads the new posts", icon: "AI" },
    { key: "alerts", title: "Discord alerts", icon: "✉" },
  ];
  const PHASES = [
    { key: "read", title: "Read sources", kinds: ["x", "instagram", "website"] },
    { key: "claude", title: "Claude", kinds: ["claude"] },
    { key: "alerts", title: "Alerts", kinds: ["alerts"] },
  ];
  const FINISHED = new Set(["done", "failed", "skipped"]);
  const RUN_STATE = { running: "Scanning", ok: "Done", partial: "Done, with some errors", error: "Stopped" };

  const S = {
    run: null, steps: new Map(), rev: 0, logId: 0, logCount: 0, totals: {}, est: null,
    offset: 0, fetchedAt: Date.now(), groupEls: new Map(), touched: new Set(), rendering: false,
  };

  // ---------- helpers
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (n) => Number(n || 0).toLocaleString("en-US");
  const serverNow = () => Date.now() + S.offset;
  const ms = (iso) => (iso ? Date.parse(iso) : NaN);
  function dur(sec) {
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60), s = sec % 60;
    if (m < 60) return `${m}m ${String(s).padStart(2, "0")}s`;
    return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
  }
  function clock(sec) {
    sec = Math.max(0, Math.floor(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(s).padStart(2, "0")}`;
  }
  const stepSecs = (s) => {
    const a = ms(s.started_at);
    if (isNaN(a)) return 0;
    const b = s.finished_at ? ms(s.finished_at) : serverNow();
    return (b - a) / 1000;
  };
  const running = () => S.run && S.run.status === "running";
  const usd = (v, d = 2) => `$${Number(v || 0).toFixed(d)}`;

  // ---------- state
  function apply(snap) {
    if (!snap || snap.waiting) return;
    if (!S.run || S.run.id !== snap.run.id) { S.steps.clear(); S.rev = 0; S.logId = 0; S.logCount = 0; $("[data-log]").textContent = ""; }
    S.run = snap.run;
    root.dataset.run = snap.run.id;
    for (const s of snap.steps) S.steps.set(s.id, s);
    S.rev = Math.max(S.rev, snap.rev || 0);
    S.totals = snap.totals || {};
    S.est = snap.estimate;
    S.offset = Date.parse(snap.server_now) - Date.now();
    S.fetchedAt = Date.now();
    appendLog(snap.log || []);
    render(snap.steps.map((s) => s.kind));
  }

  // ---------- render: header
  function renderHead() {
    const r = S.run;
    const state = $("[data-state]");
    state.textContent = RUN_STATE[r.status] || r.status;
    state.className = `live-state is-${r.status}`;
    const end = r.finished_at ? ms(r.finished_at) : serverNow();
    $("[data-elapsed]").textContent = clock((end - ms(r.started_at)) / 1000);
    $("[data-now]").textContent = running() ? (r.current || "Starting…") : (r.summary || r.current || "");
    const sub = $("[data-now-sub]");
    if (running() && r.current_at) sub.textContent = `for ${dur((serverNow() - ms(r.current_at)) / 1000)}`;
    else sub.textContent = r.dry_run ? "Dry run: nothing was changed or sent." : "";

    const frac = running() ? (S.est ? S.est.fraction : 0) : 1;
    const pct = Math.round(frac * 100);
    const fill = $("[data-bar-fill]");
    if (!S.painted) fill.style.transition = "none";      // first paint: show the real width at once, no grow-in
    fill.style.width = `${Math.max(pct, running() ? 2 : 0)}%`;
    if (!S.painted) { void fill.offsetWidth; fill.style.transition = ""; S.painted = true; }
    $("[data-bar]").setAttribute("aria-valuenow", pct);
    $("[data-bar]").classList.toggle("is-running", running());
    $("[data-pct]").textContent = `${pct}%`;
    let eta = "";
    if (running()) {
      if (S.est && S.est.eta != null) {
        const left = S.est.eta - (Date.now() - S.fetchedAt) / 1000;
        eta = left > 90 ? `about ${Math.round(left / 60)} min left` : left > 5 ? `about ${Math.round(left)}s left` : "almost done";
      } else eta = "estimating time left…";
    } else if (r.finished_at) {
      eta = `finished at ${new Date(ms(r.finished_at)).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}`;
    }
    $("[data-eta]").textContent = eta;
    $("[data-cost]").textContent = `${usd(r.apify_usd)} Apify · ${usd(r.claude_usd, 3)} Claude`;
  }

  // ---------- render: phases + stats
  function renderPhases() {
    const steps = [...S.steps.values()];
    const html = PHASES.map((p) => {
      const ps = steps.filter((s) => p.kinds.includes(s.kind));
      if (!ps.length) return "";
      let done, total;
      if (p.key === "claude") { done = S.totals.classified || 0; total = S.totals.to_classify || 0; }
      else if (p.key === "alerts") { done = S.totals.alerts_sent || 0; total = S.totals.alerts || 0; }
      else { done = ps.filter((s) => FINISHED.has(s.status)).length; total = ps.length; }
      const allFinished = ps.every((s) => FINISHED.has(s.status));
      const anyRunning = ps.some((s) => s.status === "running");
      const failed = ps.filter((s) => s.status === "failed").length;
      const state = anyRunning ? "running" : allFinished ? (failed ? "warn" : "done") : "pending";
      const fill = allFinished ? 100 : total ? Math.round((done / total) * 100) : 0;
      let count = total ? `${num(done)} / ${num(total)}` : allFinished ? "nothing to do" : "waiting";
      if (p.key === "claude" && total) count += " posts";
      if (p.key === "alerts" && allFinished && !total) count = "none";
      return `<li class="phase is-${state}">
        <span class="phase-top"><span class="phase-dot" aria-hidden="true"></span><span class="phase-title">${p.title}</span></span>
        <span class="phase-bar"><span style="width:${fill}%"></span></span>
        <span class="phase-count">${count}${failed ? ` · <b class="bad">${failed} failed</b>` : ""}</span>
      </li>`;
    }).join("");
    $("[data-phases]").innerHTML = html;

    const t = S.totals;
    const stats = [
      ["New posts", num(t.posts), "& changed pages"],
      ["Read by Claude", t.to_classify ? `${num(t.classified)}<small> / ${num(t.to_classify)}</small>` : num(t.classified), ""],
      ["New shows", num(t.shows), t.updates ? `+ ${num(t.updates)} update${t.updates === 1 ? "" : "s"}` : ""],
      ["Alerts sent", t.alerts ? `${num(t.alerts_sent)}<small> / ${num(t.alerts)}</small>` : "0", ""],
    ];
    $("[data-stats]").innerHTML = stats.map(([k, v, sub]) =>
      `<div><dt>${k}</dt><dd>${v}</dd>${sub ? `<span class="stat-sub">${sub}</span>` : ""}</div>`).join("");
  }

  // ---------- render: step groups
  function stepSide(s) {
    const t = s.finished_at || s.status === "running" ? dur(stepSecs(s)) : "";
    const time = s.status === "running" ? `<span data-since="${esc(s.started_at)}">${t}</span>` : `<span>${t}</span>`;
    let main = "";
    if (s.kind === "claude") {
      if (s.total) main = `${num(s.done)}/${num(s.total)}`;
      if (FINISHED.has(s.status) && s.shows) main = `${s.shows} show${s.shows === 1 ? "" : "s"}`;
    } else if (s.kind === "website") {
      main = s.status === "running" && s.total > 1 ? `page ${s.done + 1}/${s.total}` : FINISHED.has(s.status) && s.status !== "skipped" ? (s.posts ? `${s.posts} changed` : "same") : "";
    } else if (s.kind === "alerts") {
      main = s.total ? `${s.done}/${s.total}` : "";
    } else if (FINISHED.has(s.status) && s.status !== "skipped") {
      main = `${num(s.posts)} new`;
    }
    return `<span class="st-side">${main ? `<b>${main}</b>` : ""}${time}</span>`;
  }

  function stepRow(s) {
    const sub = [s.sublabel, s.detail].filter(Boolean).join(" · ");
    const bar = s.kind === "claude" && s.status === "running" && s.total
      ? `<span class="st-bar"><span style="width:${Math.round((s.done / s.total) * 100)}%"></span></span>` : "";
    return `<li class="step st-${s.status}" data-step="${s.id}">
      <span class="st-icon" aria-label="${s.status}"></span>
      <span class="st-main">
        <span class="st-label">${esc(s.label)}</span>
        ${sub ? `<span class="st-sub">${esc(sub)}</span>` : ""}
        ${bar}
        ${s.retries ? `<span class="st-retry" title="Tried again after a timeout, server error or network error">↻ tried again ${s.retries === 1 ? "once" : `${s.retries} times`}</span>` : ""}
        ${s.error ? `<span class="st-err">${esc(s.error)}</span>` : ""}
      </span>
      ${stepSide(s)}
    </li>`;
  }

  function renderGroups(changedKinds) {
    const host = $("[data-groups]");
    const steps = [...S.steps.values()].sort((a, b) => a.position - b.position);
    for (const g of GROUPS) {
      const gs = steps.filter((s) => s.kind === g.key);
      if (!gs.length) continue;
      let el = S.groupEls.get(g.key);
      if (!el) {
        el = document.createElement("details");
        el.className = "card group-steps";
        el.dataset.group = g.key;
        el.innerHTML = `<summary></summary><ol class="steps"></ol>`;
        el.addEventListener("toggle", () => { if (!S.rendering) S.touched.add(g.key); });
        host.append(el);
        S.groupEls.set(g.key, el);
      } else if (changedKinds && !changedKinds.has(g.key)) {
        continue;
      }
      const fin = gs.filter((s) => FINISHED.has(s.status)).length;
      const run = gs.filter((s) => s.status === "running");
      const failed = gs.filter((s) => s.status === "failed").length;
      const skipped = gs.filter((s) => s.status === "skipped").length;
      const retried = gs.filter((s) => s.retries).length;
      const state = run.length ? "running" : fin === gs.length ? (failed ? "warn" : "done") : "pending";
      const pct = Math.round((fin / gs.length) * 100);
      const now = run.length ? `<span class="g-now">${esc(run.map((s) => s.label).slice(0, 2).join(", "))}${run.length > 2 ? ` +${run.length - 2}` : ""}</span>` : "";
      el.querySelector("summary").innerHTML = `
        <span class="g-icon is-${state}" aria-hidden="true">${g.icon}</span>
        <span class="g-main">
          <span class="g-title">${g.title}</span>
          <span class="g-meta">${fin} of ${gs.length} done${failed ? ` · <b class="bad">${failed} failed</b>` : ""}${skipped ? ` · ${skipped} skipped` : ""}${retried ? ` · ${retried} retried` : ""}</span>
          ${now}
        </span>
        <span class="g-bar"><span style="width:${pct}%"></span></span>
        <svg class="g-chev" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>`;
      el.querySelector(".steps").innerHTML = gs.map(stepRow).join("");
      if (!S.touched.has(g.key)) {
        S.rendering = true;
        el.open = run.length > 0 || (failed > 0 && !running()) || gs.length <= 2;
        S.rendering = false;
      }
      if (run.length && el.open && !S.touched.has(g.key)) {
        const row = el.querySelector(".st-running");
        const list = el.querySelector(".steps");
        if (row && list.scrollHeight > list.clientHeight) list.scrollTop = row.offsetTop - list.offsetTop - 60;
      }
    }
  }

  // ---------- log
  function appendLog(lines) {
    if (!lines.length) return;
    const box = $("[data-log]");
    const follow = $("[data-follow]").checked;
    const frag = document.createDocumentFragment();
    for (const l of lines) {
      if (l.id <= S.logId) continue;
      S.logId = l.id;
      S.logCount++;
      const li = document.createElement("li");
      li.className = `lv-${l.level.toLowerCase()}`;
      const t = new Date(ms(l.at)).toLocaleTimeString([], { hour12: false });
      li.innerHTML = `<time>${t}</time><span>${esc(l.message)}</span>`;
      frag.append(li);
    }
    box.append(frag);
    while (box.children.length > 1500) box.firstChild.remove();
    $("[data-log-count]").textContent = `${num(S.logCount)} line${S.logCount === 1 ? "" : "s"}`;
    if (follow) box.scrollTop = box.scrollHeight;
  }
  $("[data-log]").addEventListener("scroll", (e) => {
    const box = e.currentTarget;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
    $("[data-follow]").checked = atBottom;
  });
  $("[data-follow]").addEventListener("change", (e) => {
    if (e.target.checked) $("[data-log]").scrollTop = $("[data-log]").scrollHeight;
  });

  function render(changedKinds) {
    if (!S.run) return;
    renderHead();
    renderPhases();
    renderGroups(changedKinds ? new Set(changedKinds) : null);
  }

  // ---------- clock-only refresh between polls
  function tick() {
    if (!S.run) return;
    renderHead();
    for (const el of root.querySelectorAll("[data-since]")) el.textContent = dur((serverNow() - ms(el.dataset.since)) / 1000);
  }

  // ---------- polling
  let timer = null;
  async function poll() {
    timer = null;
    const params = new URLSearchParams(S.run
      ? { run: S.run.id, rev: S.rev, log: S.logId }
      : { after: root.dataset.after || 0 });
    let ok = true;
    try {
      const res = await fetch(`/scan/progress?${params}`, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } });
      if (res.status === 401) { location.reload(); return; }
      if (!res.ok) throw new Error(res.status);
      const snap = await res.json();
      if (snap.waiting) {
        $("[data-now]").textContent = "Starting the scan…";
        $("[data-now-sub]").textContent = "Waiting for it to begin (a few seconds).";
      } else {
        apply(snap);
      }
    } catch (err) {
      ok = false;
      $("[data-now-sub]").textContent = "Connection lost for a moment, retrying…";
    }
    if (!S.run || running()) {
      const wait = !ok ? 5000 : document.hidden ? 5000 : 1500;
      timer = setTimeout(poll, wait);
    } else {
      finished();
    }
  }

  function finished() {
    clearInterval(ticker);
    tick();
    const note = document.createElement("p");
    note.className = "live-done";
    note.innerHTML = `<a href="/shows">See the shows</a>` +
      (location.pathname === "/scans" ? ` · <a href="/scans/${S.run.id}">This scan's full log</a>` : "");
    if (!root.querySelector(".live-done")) root.querySelector(".live").append(note);
    if (S.run.status === "error" && S.run.resumable && !root.querySelector(".live-resume")) {
      const box = document.createElement("form");
      box.className = "live-resume";
      box.method = "post";
      box.action = `/scans/${S.run.id}/resume`;
      box.innerHTML = `<p>${esc(S.run.errors || "The scan stopped partway.")}</p>
        <button class="btn-primary btn-block">Resume</button>
        <span class="muted small">Reads only the sources it didn't finish, then Claude and the alerts.</span>`;
      note.before(box);
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && timer && (!S.run || running())) { clearTimeout(timer); poll(); }
  });

  // ---------- start
  let init = null;
  try { init = JSON.parse($("[data-progress-init]").textContent); } catch (e) { init = null; }
  if (init) apply(init);
  const ticker = setInterval(tick, 1000);
  if (!init || running()) timer = setTimeout(poll, init ? 1200 : 600);
  else finished();
})();
