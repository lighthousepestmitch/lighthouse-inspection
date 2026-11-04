/* Lighthouse tools: opens offline and shows the "new version" banner. Written once, loaded by Home, Inspection Capture, Job Calendar and Service Records.
   - Registers sw.js (same folder as this file). Waits for the page to finish loading and swallows every error, so it can never stop a page starting.
   - Does nothing on file:// or localhost (so a preview isn't stuck on a saved copy) unless the address has ?sw=1.
   - A new sw.js never takes over by itself. When one is waiting this shows "A new version is ready. Tap to update."
     Only that tap lets the waiting worker take over, and only then does the page reload. No automatic reload, ever.
   - ?nosw=1 on any page: unregister this folder's worker, delete the lpc- caches, stay off on this device, reload without it. ?nosw=0 turns it back on.
   - Hooks a page can set: window.lpcSwBeforeUpdate() runs on the tap (return false, or a promise of false, to hold the update back, e.g. unsaved work).
     An element with id="lpc-sw-status" gets a quiet line saying whether this device opens offline, with the switch on / off link. */
(function () {
  "use strict";
  var FLAG = "lpc_nosw";
  var PAGES = ["home.html", "index.html", "calendar.html", "records.html"];     // the same four pages sw.js saves
  var me = document.currentScript || document.querySelector('script[src*="lpc-sw-banner.js"]');
  var BASE;
  try { BASE = new URL("./", me && me.src ? me.src : location.href); } catch (e) { return; }
  var banner = null, dismissed = false, tapped = false, reloading = false, lastCheck = Date.now();

  function param(n) { try { return new URLSearchParams(location.search).get(n); } catch (e) { return null; } }
  function off(v) {      // no argument: is it switched off on this device? true / false: set that mark
    try {
      if (v === undefined) return localStorage.getItem(FLAG) === "1";
      if (v) localStorage.setItem(FLAG, "1"); else localStorage.removeItem(FLAG);
    } catch (e) { /* private window: the mark can't be kept, so it comes back on at the next load */ }
    return false;
  }
  function addressWith(name, value) {
    var u = new URL(location.href);
    if (value == null) u.searchParams.delete(name); else u.searchParams.set(name, value);
    return u.href;
  }
  function whenReady(fn) { if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn); else fn(); }

  // Forget this folder's worker and the lpc- caches (guides/ and sds/ have their own, with their own names: not touched).
  function wipe() {
    var jobs = [];
    try {
      jobs.push(navigator.serviceWorker.getRegistrations().then(function (rs) {
        return Promise.all(rs.filter(function (r) { return r.scope === BASE.href; }).map(function (r) { return r.unregister(); }));
      }));
    } catch (e) { /* no service worker in this browser */ }
    try {
      jobs.push(caches.keys().then(function (ks) {
        return Promise.all(ks.filter(function (k) { return k.indexOf("lpc-") === 0; }).map(function (k) { return caches.delete(k); }));
      }));
    } catch (e) { /* no cache storage here */ }
    return Promise.all(jobs.map(function (p) { return p.catch(function () {}); }));
  }

  var kill = param("nosw");
  if (kill === "1") {
    off(true);
    var back = function () { location.replace(addressWith("nosw", null)); };
    wipe().then(back, back);
    return;
  }
  if (kill === "0") {
    off(false);
    try { history.replaceState(null, "", addressWith("nosw", null)); } catch (e) { /* the address keeps ?nosw=0, harmless */ }
  }
  if (!("serviceWorker" in navigator)) return;
  var local = location.protocol === "file:" || /^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname);
  if (local && param("sw") !== "1") return;
  if (off()) { whenReady(function () { say("off"); }); return; }

  /* ----- the quiet line on Home: is this device ready to open offline ----- */
  function say(state, ver) {
    var el = document.getElementById("lpc-sw-status");
    if (!el) return;
    el.textContent = "";
    var a = document.createElement("a");
    if (state === "off") {
      el.appendChild(document.createTextNode("Offline copy is off on this device. "));
      a.textContent = "Switch on"; a.href = addressWith("nosw", "0");
    } else {
      el.appendChild(document.createTextNode(state === "ready" ? "Opens offline (" + ver + "). " : "Offline copy is still setting up. Open each tool once with signal. "));
      a.textContent = "Switch off"; a.href = addressWith("nosw", "1");
      a.onclick = function (ev) { if (!confirm("Switch off the offline copy on this device? You can switch it back on from here.")) ev.preventDefault(); };
    }
    el.appendChild(a);
  }
  function check() {
    if (!window.caches) return;
    caches.keys().then(function (ks) {
      var k = ks.filter(function (n) { return n.indexOf("lpc-app-") === 0; }).sort().pop();
      if (!k) { say("setup"); return; }
      return caches.open(k).then(function (c) {
        return Promise.all(PAGES.map(function (p) { return c.match(new URL(p, BASE).href); }));
      }).then(function (found) { say(found.every(Boolean) ? "ready" : "setup", k.slice(8)); });
    }).catch(function () {});
  }

  /* ----- the banner ----- */
  function css() {
    if (document.getElementById("lpc-upd-css")) return;
    var s = document.createElement("style");
    s.id = "lpc-upd-css";
    s.textContent = ".lpc-upd{position:fixed;left:50%;bottom:calc(12px + env(safe-area-inset-bottom));transform:translateX(-50%);z-index:100000;display:flex;align-items:stretch;" +
      "max-width:calc(100vw - 24px);background:var(--deep,var(--timber,#0E4F54));color:var(--deepInk,#fff);border-radius:12px;box-shadow:0 4px 18px rgba(20,32,30,.28);" +
      "font:600 14px/1.3 'Public Sans',system-ui,-apple-system,sans-serif}" +
      ".lpc-upd button{color:inherit;font:inherit;background:none;border:0;cursor:pointer;min-height:44px}" +
      ".lpc-upd .go{padding:8px 6px 8px 14px;text-align:left}.lpc-upd .no{padding:0 14px 0 10px;font-size:20px;font-weight:400;opacity:.85}";
    document.head.appendChild(s);
  }
  function offer(worker) {
    if (banner || dismissed) return;
    css();
    banner = document.createElement("div");
    banner.className = "lpc-upd";
    banner.setAttribute("role", "status");
    var go = document.createElement("button");
    go.type = "button"; go.className = "go"; go.textContent = "A new version is ready. Tap to update.";
    go.onclick = function () { update(worker); };
    var no = document.createElement("button");
    no.type = "button"; no.className = "no"; no.textContent = "×"; no.setAttribute("aria-label", "Not now");
    no.onclick = function () { dismissed = true; if (banner && banner.parentNode) banner.parentNode.removeChild(banner); banner = null; };
    banner.appendChild(go); banner.appendChild(no);
    (document.body || document.documentElement).appendChild(banner);
  }
  function reload() { if (!tapped || reloading) return; reloading = true; location.reload(); }
  // The tap. The page may hold it back (unsaved work); otherwise the waiting worker is told to take over and the page reloads once it has.
  function update(worker) {
    var held;
    try { held = window.lpcSwBeforeUpdate ? window.lpcSwBeforeUpdate() : true; } catch (e) { held = true; }
    Promise.resolve(held).then(function (v) { return v; }, function () { return true; }).then(function (v) {
      if (v === false) return;
      tapped = true;
      navigator.serviceWorker.addEventListener("controllerchange", reload);
      worker.postMessage({ type: "SKIP" });
      setTimeout(reload, 4000);       // still the person's own tap: if the switch-over event never comes, reload anyway
    });
  }

  /* ----- register, and watch for a new version waiting ----- */
  function watch(reg) {
    var sw = navigator.serviceWorker;
    if (reg.waiting && sw.controller) offer(reg.waiting);
    reg.addEventListener("updatefound", function () {
      var w = reg.installing;
      if (w) w.addEventListener("statechange", function () { if (w.state === "installed" && sw.controller) offer(w); });
    });
    // A Home Screen app can stay open for days: look for a new sw.js when the person comes back to it (at most every 10 minutes).
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible" && Date.now() - lastCheck > 600000) { lastCheck = Date.now(); reg.update().catch(function () {}); }
    });
    sw.ready.then(function () { whenReady(check); }, function () {});
  }
  function start() {
    try {
      navigator.serviceWorker.register(new URL("sw.js", BASE).href, { scope: BASE.pathname }).then(watch, function () {});
    } catch (e) { /* never in the way */ }
  }
  if (document.readyState === "complete") setTimeout(start, 0);
  else window.addEventListener("load", function () { setTimeout(start, 0); });
})();
