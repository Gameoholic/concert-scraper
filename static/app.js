// Small enhancements on top of plain forms: everything works without this file.
document.documentElement.classList.add("js");

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST", body, credentials: "same-origin",
    headers: { "X-Requested-With": "fetch" },
  });
  if (res.status === 401) { location.reload(); throw new Error("signed out"); }
  return res;
}

// Swap [data-live] regions (artist list, detail header) with fresh copies from the server.
async function refreshLive() {
  const res = await fetch(location.href, { credentials: "same-origin" });
  if (!res.ok) return;
  const doc = new DOMParser().parseFromString(await res.text(), "text/html");
  for (const el of $$("[data-live][id]")) {
    const fresh = doc.getElementById(el.id);
    if (!fresh) continue;
    const scroll = el.scrollTop;
    el.replaceWith(fresh);
    fresh.scrollTop = scroll;
  }
  initFilter();
}

// ---------- live search on the artist list
function initFilter() {
  const input = $("[data-filter]");
  if (!input || input.dataset.bound) return;
  input.dataset.bound = "1";
  const fold = (s) => s.normalize("NFKC").toLowerCase();
  const apply = () => {
    const q = fold(input.value.trim());
    let any = false;
    for (const group of $$("[data-group]")) {
      let shown = 0;
      for (const row of $$(".row", group)) {
        const hit = !q || row.dataset.search.includes(q);
        row.hidden = !hit;
        if (hit) shown++;
      }
      group.hidden = shown === 0;
      const count = $("[data-count]", group);
      if (count) count.textContent = shown;
      if (shown) any = true;
    }
    const empty = $("[data-empty]");
    if (empty) empty.hidden = any;
  };
  input.addEventListener("input", apply);
  input.form.addEventListener("submit", (e) => { e.preventDefault(); apply(); input.blur(); });
  if (input.value) apply();
}
initFilter();

document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
    const input = $("[data-filter]");
    if (input && input.offsetParent) { e.preventDefault(); input.focus(); }
  }
});

// ---------- paste box: live preview of what each line becomes
const pasteForm = $("[data-paste]");
if (pasteForm) {
  const area = $("textarea", pasteForm);
  const target = $("[data-preview]", pasteForm);
  let seq = 0;

  const currentPicks = () => {
    const picks = {};
    for (const el of $$("[data-pick]", target)) {
      const checked = el.tagName === "SELECT" ? el.value : $("input:checked", el)?.value;
      if (checked) picks[el.dataset.pick] = checked;
    }
    return picks;
  };

  const refresh = async () => {
    const mine = ++seq;
    const picks = currentPicks();
    const body = new FormData();
    body.set("paste", area.value);
    const res = await post(area.dataset.previewUrl, body);
    if (mine !== seq || !res.ok) return;
    target.innerHTML = await res.text();
    for (const el of $$("[data-pick]", target)) {
      const want = picks[el.dataset.pick];
      if (!want) continue;
      if (el.tagName === "SELECT") el.value = want;
      else { const r = $(`input[value="${want}"]`, el); if (r) r.checked = true; }
    }
    $$(".error", pasteForm).forEach((e) => e.remove());
  };

  area.addEventListener("input", debounce(refresh, 250));
  area.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); pasteForm.requestSubmit(); }
  });
  target.addEventListener("click", (e) => {
    if (!e.target.closest("[data-clear]")) return;
    area.value = "";
    refresh();
    area.focus();
  });
}

// ---------- artist details: save on change
const autosave = $("[data-autosave]");
if (autosave) {
  const status = $("[data-save-status]");
  let timer, pending = Promise.resolve();
  const show = (text, isError = false) => {
    clearTimeout(timer);
    status.textContent = text;
    status.classList.toggle("is-error", isError);
    status.style.opacity = 1;
    if (!isError && text === "Saved") timer = setTimeout(() => { status.style.opacity = 0; }, 1800);
  };
  const save = (refresh) => {
    pending = pending.then(async () => {
      if (!autosave.reportValidity()) return;
      show("Saving…");
      try {
        const res = await post(autosave.action, new FormData(autosave));
        const data = await res.json();
        if (!data.ok) return show(data.error || "Couldn't save", true);
        show("Saved");
        if (refresh) refreshLive();
      } catch (err) {
        show("Couldn't save. Check your connection.", true);
      }
    });
  };
  autosave.addEventListener("change", (e) => save(e.target.name !== "notes"));
  $("textarea[name=notes]", autosave)?.addEventListener("input", debounce(() => save(false), 900));
  autosave.addEventListener("submit", (e) => { e.preventDefault(); save(true); });
}

// ---------- confirm before deleting
const dialog = $("#confirm");
document.addEventListener("submit", (e) => {
  const form = e.target;
  if (!form.dataset || !form.dataset.confirm || !dialog || form.dataset.confirmed) return;
  e.preventDefault();
  $("[data-confirm-text]", dialog).textContent = form.dataset.confirm;
  $("[data-confirm-ok]", dialog).textContent = form.dataset.confirmOk || "Delete";
  dialog.returnValue = "";
  dialog.showModal();
  dialog.addEventListener("close", () => {
    if (dialog.returnValue === "ok") { form.dataset.confirmed = "1"; form.submit(); }
  }, { once: true });
});

// ---------- source menus: one open at a time, close on outside click / Escape
document.addEventListener("click", (e) => {
  for (const m of $$("[data-menu][open]")) if (!m.contains(e.target)) m.open = false;
});
document.addEventListener("toggle", (e) => {
  if (e.target.matches?.("[data-menu]") && e.target.open) {
    for (const m of $$("[data-menu][open]")) if (m !== e.target) m.open = false;
    $("input", e.target)?.focus();
  }
}, true);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") for (const m of $$("[data-menu][open]")) { m.open = false; $("summary", m).focus(); }
});

// ---------- toast
const toast = $("[data-toast]");
if (toast) setTimeout(() => toast.classList.add("gone"), 3200);

// ---------- trip: add / remove stays
const trip = $("[data-trip]");
if (trip) {
  const rows = $("[data-trip-rows]", trip);
  const tpl = $("[data-trip-template]", trip);
  $("[data-trip-add]", trip).addEventListener("click", () => {
    const last = $$("[data-trip-row]", rows).pop();
    const row = tpl.content.firstElementChild.cloneNode(true);
    const prevEnd = last && $("input[name=end]", last).value;
    if (prevEnd) $("input[name=start]", row).value = prevEnd;   // next stay starts where the last one ends
    rows.append(row);
    $("input[name=city]", row).focus();
  });
  rows.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-trip-remove]");
    if (btn && $$("[data-trip-row]", rows).length > 1) btn.closest("[data-trip-row]").remove();
  });
}

// ---------- while a scan runs: show progress, reload when it's done
if ($(".live-dot") && !$("[data-progress]")) {
  const summary = $("[data-scan-summary]");
  const poll = async () => {
    try {
      const res = await fetch("/scan/status", { credentials: "same-origin" });
      if (!res.ok) return;
      const data = await res.json();
      if (!data.running) { location.reload(); return; }
      if (summary) summary.textContent = data.summary;
    } catch (err) { /* offline for a moment: try again */ }
    setTimeout(poll, 4000);
  };
  setTimeout(poll, 3000);
}
