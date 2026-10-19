/* Lighthouse SDS service worker. Scope is this folder only (Records, Job Book and Calendar stay out of it).
   Cache rules:
   1. sds-pdf-v1 holds the PDFs under /sds/pdf/. Cache first on the exact URL (the ?v= date is part of it), network on a miss.
   2. sds-shell-v1 holds the page, the manifest and the two JSON files. Network first, 4 second wait, fresh copy saved
      under the URL with no query string, saved copy used when the network is slow or gone.
   3. Anything else (other sites, other pages, non-GET) goes straight through untouched.
   Old "sds-" caches are cleared when a new worker takes over. */
"use strict";

const SHELL = "sds-shell-v1";
const PDFS = "sds-pdf-v1";
const WAIT_MS = 4000;
const BASE = new URL("./", self.location.href);                      // the sds/ folder
const INDEX = new URL("index.html", BASE).href;
const MANIFEST_PATH = new URL("manifest.webmanifest", BASE).pathname;
const JSON_FILES = ["../jobbook-data/chemicals.json", "../jobbook-data/sds.json"];

self.addEventListener("install", function (e) {
  e.waitUntil((async function () {
    const cache = await caches.open(SHELL);
    await cache.addAll(["./", "index.html", "manifest.webmanifest"].map(function (u) {
      return new Request(new URL(u, BASE).href, { cache: "reload" });
    }));
    // The data files are saved too, but a hiccup here must not stop the worker installing.
    await Promise.all(JSON_FILES.map(async function (u) {
      try {
        const abs = new URL(u, BASE).href;
        const r = await fetch(abs, { cache: "reload" });
        if (r.ok) await cache.put(abs, r);
      } catch (err) { /* the page saves them on its next visit */ }
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", function (e) {
  e.waitUntil((async function () {
    const names = await caches.keys();
    await Promise.all(names.map(function (k) {
      return (k.indexOf("sds-") === 0 && k !== SHELL && k !== PDFS) ? caches.delete(k) : null;
    }));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", function (e) {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const p = url.pathname;

  if (p.indexOf("/sds/pdf/") >= 0 && p.toLowerCase().endsWith(".pdf")) {
    e.respondWith(pdfFirst(e, req));
    return;
  }
  const isNav = req.mode === "navigate" && p.indexOf(BASE.pathname) === 0;
  const isJson = p.endsWith("/jobbook-data/chemicals.json") || p.endsWith("/jobbook-data/sds.json");
  if (isNav || isJson || p === MANIFEST_PATH) {
    e.respondWith(netFirst(e, req, isNav, isJson));
  }
});

function keep(e, cache, key, res) {
  const job = cache.put(key, res).catch(function () {});
  try { e.waitUntil(job); } catch (err) { /* worker already winding down, the put still runs */ }
}

/* PDFs: saved copy first. Only a full 200 is stored (a 206 slice can't be). */
async function pdfFirst(e, req) {
  const cache = await caches.open(PDFS);
  const hit = await cache.match(req.url, { ignoreVary: true });
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok && res.status === 200) keep(e, cache, req.url, res.clone());
  return res;
}

/* Page, manifest, JSON: network first with a 4 second wait. */
async function netFirst(e, req, isNav, isJson) {
  const cache = await caches.open(SHELL);
  const u = new URL(req.url);
  const key = u.origin + u.pathname;                                  // no query string
  const saved = async function () {
    const c = (await cache.match(key, { ignoreVary: true })) || (await cache.match(req.url, { ignoreSearch: true, ignoreVary: true }));
    if (!c || !isJson) return c;
    // Tell the page this JSON is the saved copy, so it can show "Offline".
    const h = new Headers(c.headers); h.set("x-sds-cache", "1");
    return new Response(c.body, { status: c.status, statusText: c.statusText, headers: h });
  };

  const net = fetch(req).then(function (res) {
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
