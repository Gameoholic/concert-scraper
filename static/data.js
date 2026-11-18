// The scan page's "What this scan read": loads its list the first time it's opened, then filters and pages it.
(() => {
  const box = document.querySelector("[data-data]");
  if (!box) return;
  const body = box.querySelector("[data-data-body]");
  const state = { show: "", artist: "0", loaded: false };

  async function get(offset) {
    const q = new URLSearchParams({ show: state.show, artist: state.artist, offset: String(offset) });
    const res = await fetch(`${box.dataset.data}?${q}`, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } });
    if (res.status === 401) { location.reload(); return ""; }
    if (!res.ok) throw new Error(res.status);
    return res.text();
  }

  async function load() {
    body.setAttribute("aria-busy", "true");
    try {
      body.innerHTML = await get(0);
      state.loaded = true;
    } catch (e) {
      body.innerHTML = `<p class="error data-loading">Couldn't load it. <button type="button" class="btn-quiet" data-retry>Try again</button></p>`;
    }
    body.removeAttribute("aria-busy");
  }

  box.addEventListener("toggle", () => { if (box.open && !state.loaded) load(); });

  body.addEventListener("click", async (e) => {
    const chip = e.target.closest("[data-filter]");
    if (chip) { state.show = chip.dataset.filter; load(); return; }
    if (e.target.closest("[data-retry]")) { load(); return; }
    const more = e.target.closest("[data-more]");
    if (more) {
      more.disabled = true;
      more.textContent = "Loading…";
      try {
        const html = await get(Number(more.dataset.more));
        const li = more.closest("li");
        li.insertAdjacentHTML("beforebegin", html);
        li.remove();
      } catch (err) {
        more.disabled = false;
        more.textContent = "Couldn't load more. Try again";
      }
    }
  });

  body.addEventListener("change", (e) => {
    if (e.target.matches("[data-artist]")) { state.artist = e.target.value; load(); }
  });
})();
