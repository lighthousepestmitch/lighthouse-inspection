/* Field Guides page. Reads data/index.json and one data/<id>.json per guide, and draws them with render.js.
   Works with no signal once opened (see sw.js). Ticks are kept in this browser only. */
(function () {
  "use strict";
  const BUILD = "2026-11-04a";   // bump with VERSION in sw.js whenever the page, styles or schema change (guide text alone does not need it)
  const R = window.LPCRender;
  const $ = function (s) { return document.querySelector(s); };
  const TICK_KEY = "lpc-guides-ticks:";

  const S = {
    order: [], guides: {}, hay: {}, q: "", fromCache: false, loaded: false,
    current: null, ticks: new Set(), total: 0, listY: 0, armTimer: null, fromList: false
  };

  /* ---------- storage (never trusted: private windows and cleared data both throw) ---------- */
  function loadTicks(id) {
    try {
      const a = JSON.parse(localStorage.getItem(TICK_KEY + id) || "[]");
      return new Set(Array.isArray(a) ? a.filter(function (x) { return typeof x === "string"; }) : []);
    } catch (e) { return new Set(); }
  }
  function saveTicks() {
    try {
      if (!S.current) return;
      if (S.ticks.size) localStorage.setItem(TICK_KEY + S.current, JSON.stringify(Array.from(S.ticks)));
      else localStorage.removeItem(TICK_KEY + S.current);
    } catch (e) { /* ticks still work for this visit */ }
  }

  /* ---------- loading ---------- */
  async function getJSON(path) {
    try {
      const r = await fetch(path + "?v=" + BUILD);
      if (!r.ok) return null;
      return { data: await r.json(), cached: r.headers.get("x-guides-cache") === "1" };
    } catch (e) { return null; }
  }

  function fail() {
    const b = $("#boot"); b.hidden = false; $("#vHome").hidden = true;
    b.textContent = "";
    const p = document.createElement("p");
    p.textContent = navigator.onLine === false
      ? "You're offline and this iPad hasn't saved the guides yet. Open Field Guides once with signal and it will keep a copy."
      : "The guides didn't come down. Check your signal and try again.";
    const btn = document.createElement("button");
    btn.className = "btn primary"; btn.type = "button"; btn.textContent = "Try again";
    btn.addEventListener("click", function () { location.reload(); });
    b.append(p, btn);
  }

  async function load() {
    const a = await getJSON("data/index.json");
    if (!a || !a.data || !Array.isArray(a.data.guides)) { if (!S.loaded) fail(); return; }
    S.fromCache = a.cached;
    S.order = a.data.guides.filter(function (g) { return g && /^[a-z0-9-]+$/.test(g.id || "") && g.title; })
      .sort(function (x, y) { return (x.order || 0) - (y.order || 0); });
    await Promise.all(S.order.map(async function (e) {
      const r = await getJSON("data/" + e.id + ".json");
      if (r && r.data && Array.isArray(r.data.sections)) {
        S.guides[e.id] = r.data;
        S.hay[e.id] = R.searchText(r.data, e);
        if (r.cached) S.fromCache = true;
      } else {
        S.hay[e.id] = R.searchText(null, e);
      }
    }));
    S.loaded = true;
    $("#boot").hidden = true;
    const newest = S.order.reduce(function (m, e) { return e.updated > m ? e.updated : m; }, "");
    $("#build").textContent = "Field Guides, build " + BUILD + (newest ? ". Guides updated " + R.fmtDate(newest) + "." : "");
    renderHome();
    route();
  }

  /* ---------- home ---------- */
  function renderHome() {
    const toks = S.q.toLowerCase().split(/\s+/).filter(Boolean);
    const shown = S.order.filter(function (e) { return toks.every(function (t) { return (S.hay[e.id] || "").indexOf(t) >= 0; }); });
    const list = $("#list");
    if (!shown.length) {
      list.textContent = "";
      const d = document.createElement("div"); d.className = "empty";
      d.textContent = S.q.trim() ? "No guides match “" + S.q.trim() + "”." : "No guides to show yet.";
      list.append(d);
    } else {
      list.innerHTML = R.renderCards(shown, { available: function (id) { return !!S.guides[id]; } });
    }
    $("#off").hidden = !(S.loaded && (navigator.onLine === false || S.fromCache));
  }

  /* ---------- guide view ---------- */
  function setHeaderHeight() {
    const h = $("#top");
    if (h) document.documentElement.style.setProperty("--hh", h.offsetHeight + "px");
  }
  function updateProg() {
    const n = document.querySelectorAll("#vGuide input[data-k]:checked").length, t = S.total;
    document.querySelectorAll("[data-prog-txt]").forEach(function (e) { e.textContent = n + " of " + t + " ticked"; });
    const c = $("#pcount"); if (c) c.textContent = n + "/" + t;
    const b = $("#pbar"); if (b) b.style.width = t ? Math.round(100 * n / t) + "%" : "0";
  }

  function showHome() {
    S.current = null;
    document.title = "Field Guides";
    $("#vGuide").hidden = true; $("#vHome").hidden = false;
    $("#hGuide").hidden = true; $("#hHome").hidden = false;
    setHeaderHeight();
    window.scrollTo(0, S.listY || 0);
  }

  function showMissing(id) {
    const e = S.order.find(function (x) { return x.id === id; });
    S.current = null;
    $("#vHome").hidden = true; $("#vGuide").hidden = false;
    $("#hHome").hidden = true; $("#hGuide").hidden = false;
    $("#gtitle").textContent = e ? e.title : "Guide";
    $("#prog").hidden = true; $("#jump").hidden = true; $("#printBtn").hidden = true;
    const v = $("#vGuide"); v.textContent = "";
    const d = document.createElement("div"); d.className = "boot";
    d.textContent = "This guide isn't saved on this iPad yet. Open Field Guides once with signal and it will keep a copy.";
    v.append(d);
    setHeaderHeight();
    window.scrollTo(0, 0);
  }

  function openGuide(id) {
    const g = S.guides[id];
    if (!S.current) S.listY = window.scrollY || 0;
    S.current = id;
    S.ticks = loadTicks(id);
    const out = R.renderGuide(g, { ticks: S.ticks });
    S.total = out.total;
    $("#vGuide").innerHTML = out.html;
    $("#gtitle").textContent = g.title;
    document.title = g.title + " | Field Guides";
    $("#prog").hidden = !out.total;
    $("#printBtn").hidden = false;
    const jump = $("#jump"); jump.textContent = "";
    out.jumps.forEach(function (j) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "jchip"; b.textContent = j.label; b.dataset.to = j.id;
      jump.append(b);
    });
    jump.hidden = out.jumps.length < 3;
    $("#vHome").hidden = true; $("#vGuide").hidden = false;
    $("#hHome").hidden = true; $("#hGuide").hidden = false;
    updateProg();
    setHeaderHeight();
    window.scrollTo(0, 0);
  }

  function route() {
    let id = "";
    try { id = decodeURIComponent((location.hash || "").replace(/^#\/?/, "")); } catch (e) { id = ""; }
    if (!S.loaded) return;
    if (id && S.guides[id]) openGuide(id);
    else if (id && S.order.some(function (e) { return e.id === id; })) showMissing(id);
    else showHome();
  }

  /* ---------- events ---------- */
  $("#q").addEventListener("input", function (e) { S.q = e.target.value; renderHome(); });

  /* Back goes one step in history when the guide was opened from the list (keeps the list where it was), else to the list. */
  $("#back").addEventListener("click", function () {
    if (S.fromList) { S.fromList = false; history.back(); }
    else location.hash = "";
  });
  $("#list").addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("a.card")) S.fromList = true;
  });
  window.addEventListener("hashchange", route);

  $("#printBtn").addEventListener("click", function () { window.print(); });

  $("#jump").addEventListener("click", function (e) {
    const b = e.target.closest && e.target.closest("[data-to]"); if (!b) return;
    const t = document.getElementById(b.dataset.to);
    if (t) t.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  $("#vGuide").addEventListener("change", function (e) {
    const t = e.target;
    if (!t || !t.matches || !t.matches("input[data-k]")) return;
    if (t.checked) S.ticks.add(t.dataset.k); else S.ticks.delete(t.dataset.k);
    const li = t.closest("li"); if (li) li.classList.toggle("done", t.checked);
    saveTicks(); updateProg();
  });

  /* Reset ticks takes two taps so a stray touch never wipes a job half done. */
  function disarm() {
    clearTimeout(S.armTimer);
    document.querySelectorAll("[data-reset]").forEach(function (b) { delete b.dataset.arm; b.textContent = "Reset ticks"; });
  }
  $("#vGuide").addEventListener("click", function (e) {
    const b = e.target.closest && e.target.closest("[data-reset]"); if (!b) return;
    if (!b.dataset.arm) {
      document.querySelectorAll("[data-reset]").forEach(function (x) { x.dataset.arm = "1"; x.textContent = "Tap again to clear all ticks"; });
      clearTimeout(S.armTimer); S.armTimer = setTimeout(disarm, 3500);
      return;
    }
    disarm();
    S.ticks = new Set(); saveTicks();
    document.querySelectorAll("#vGuide input[data-k]").forEach(function (i) { i.checked = false; const li = i.closest("li"); if (li) li.classList.remove("done"); });
    updateProg();
  });

  window.addEventListener("resize", setHeaderHeight);
  window.addEventListener("offline", function () { if (S.loaded) renderHome(); });
  window.addEventListener("online", function () { if (S.loaded) renderHome(); });

  /* ---------- service worker, and the "new version" banner ---------- */
  function watchWorker() {
    if (!("serviceWorker" in navigator)) return;
    const had = !!navigator.serviceWorker.controller;   // first ever visit has none, so no banner then
    navigator.serviceWorker.addEventListener("controllerchange", function () {
      if (had) $("#newver").hidden = false;
    });
    navigator.serviceWorker.register("sw.js").then(function (reg) {
      document.addEventListener("visibilitychange", function () {
        if (!document.hidden) { try { reg.update().catch(function () {}); } catch (e) {} }
      });
    }).catch(function () { /* the page still works online */ });
  }
  $("#refresh").addEventListener("click", function () { location.reload(); });
  if (document.readyState === "complete") watchWorker(); else window.addEventListener("load", watchWorker);

  load();
})();
