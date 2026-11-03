/* Lighthouse Field Guides service worker. Same pattern as the SDS one. Scope is this folder only.
   Cache rules:
   1. guides-shell-v1 holds the page, its scripts and styles, the manifest, the guide data and the logo files.
      Network first with a 4 second wait, the fresh copy saved under the URL with no query string, the saved copy used when
      the network is slow or gone. Guide JSON served from the saved copy is tagged x-guides-cache: 1 so the page can say "Offline".
   2. guides-fonts-v1 keeps the Google Fonts files after the first visit, so the iPad looks the same with no signal.
      Saved copy first, refreshed in the background. If fonts were never saved the page falls back to Georgia and the iPad's own font.
   3. Anything else (other sites, other pages, non-GET) goes straight through untouched.
   Old "guides-" caches are cleared when a new worker takes over.
   VERSION must match BUILD in app.js. Bump both when the page, styles, scripts or schema change. Guide text alone needs no bump:
   it is network first, so the next visit with signal picks it up. */
"use strict";

const VERSION = "2026-11-04a";
const SHELL = "guides-shell-v1";
const FONTS = "guides-fonts-v1";
const WAIT_MS = 4000;
const BASE = new URL("./", self.location.href);                       // the guides/ folder
const INDEX = new URL("index.html", BASE).href;

// The page cannot open without these. If one is missing the worker does not install (the old one keeps working).
const CORE = ["./", "index.html", "app.js", "render.js", "guides.css", "lpc-look.css", "manifest.webmanifest", "data/index.json"];
// Saved on a best-effort basis: the page still works if one fails, and it saves them again on its next visit.
const LOGOS = ["../logo/lpc-badge.png", "../logo/icon-32.png", "../logo/icon-180.png", "../logo/icon-192.png", "../logo/icon-512.png"];
const LOGO_PATHS = LOGOS.map(function (u) { return new URL(u, BASE).pathname; });
const FONT_HOSTS = ["fonts.googleapis.com", "fonts.gstatic.com"];

self.addEventListener("install", function (e) {
  e.waitUntil((async function () {
    const cache = await caches.open(SHELL);
    await cache.addAll(CORE.map(function (u) { return new Request(new URL(u, BASE).href, { cache: "reload" }); }));
    // Every guide listed in data/index.json, so they are all there with no signal. A hiccup here must not stop the install.
    try {
      const idx = await (await cache.match(new URL("data/index.json", BASE).href)).json();
      await Promise.all((idx.guides || []).map(async function (g) {
        if (!g || !/^[a-z0-9-]+$/.test(g.id || "")) return;
        try {
          const abs = new URL("data/" + g.id + ".json", BASE).href;
          const r = await fetch(abs, { cache: "reload" });
          if (r.ok) await cache.put(abs, r);
        } catch (err) { /* the page saves it on its next visit */ }
      }));
    } catch (err) { /* same */ }
    await Promise.all(LOGOS.map(async function (u) {
      try {
        const abs = new URL(u, BASE).href;
        const r = await fetch(abs, { cache: "reload" });
        if (r.ok) await cache.put(abs, r);
      } catch (err) { /* same */ }
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", function (e) {
  e.waitUntil((async function () {
    const names = await caches.keys();
    await Promise.all(names.map(function (k) {
      return (k.indexOf("guides-") === 0 && k !== SHELL && k !== FONTS) ? caches.delete(k) : null;
    }));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", function (e) {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  if (FONT_HOSTS.indexOf(url.hostname) >= 0) {
    e.respondWith(fontsFirst(e, req));
    return;
  }
  if (url.origin !== self.location.origin) return;
  const p = url.pathname;

  if (LOGO_PATHS.indexOf(p) >= 0) {
    e.respondWith(logoFirst(e, req));
    return;
  }
  if (p.indexOf(BASE.pathname) === 0) {
    e.respondWith(netFirst(e, req, req.mode === "navigate", p.indexOf(BASE.pathname + "data/") === 0 && p.endsWith(".json")));
  }
});

function keep(e, cache, key, res) {
  const job = cache.put(key, res).catch(function () {});
  try { e.waitUntil(job); } catch (err) { /* worker already winding down, the put still runs */ }
}

/* Logos: saved copy first, network on a miss. */
async function logoFirst(e, req) {
  const cache = await caches.open(SHELL);
  const hit = await cache.match(req.url, { ignoreSearch: true, ignoreVary: true });
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok && res.status === 200) keep(e, cache, req.url, res.clone());
  return res;
}

/* Google Fonts: saved copy first, refreshed in the background. */
async function fontsFirst(e, req) {
  const cache = await caches.open(FONTS);
  const hit = await cache.match(req, { ignoreVary: true });
  const net = fetch(req).then(function (res) {
    if (res && (res.ok || res.type === "opaque")) keep(e, cache, req, res.clone());
    return res;
  });
  net.catch(function () {});
  return hit || net;
}

/* Page, scripts, styles, manifest, guide data: network first with a 4 second wait. */
async function netFirst(e, req, isNav, isJson) {
  const cache = await caches.open(SHELL);
  const u = new URL(req.url);
  const key = u.origin + u.pathname;                                  // no query string
  const saved = async function () {
    const c = (await cache.match(key, { ignoreVary: true })) || (await cache.match(req.url, { ignoreSearch: true, ignoreVary: true }));
    if (!c || !isJson) return c;
    // Tell the page this JSON is the saved copy, so it can show "Offline".
    const h = new Headers(c.headers); h.set("x-guides-cache", "1");
    return new Response(c.body, { status: c.status, statusText: c.statusText, headers: h });
  };

  const net = (isNav ? fetch(req) : fetch(req, { cache: "no-cache" })).then(function (res) {
    if (res.ok && res.status === 200 && res.type === "basic" && !res.redirected) keep(e, cache, key, res.clone());
    return res;
  });
  net.catch(function () {});                                          // quiet if it fails after we've moved on

  try {
    const first = await Promise.race([net, new Promise(function (r) { setTimeout(function () { r(null); }, WAIT_MS); })]);
    if (first) {
      if (first.ok || first.type === "opaqueredirect") return first;
      return (await saved()) || first;                                // server error: prefer the saved copy
    }
    const c = await saved();                                          // 4 seconds up, network still going
    if (c) return c;
    return await net;                                                 // nothing saved yet, so keep waiting
  } catch (err) {
    const c = await saved();
    if (c) return c;
    if (isNav) {
      const i = await cache.match(INDEX, { ignoreVary: true });
      if (i) return i;
    }
    return Response.error();
  }
}
