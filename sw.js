/* Lighthouse tools service worker: opens Home, Inspection Capture, Job Calendar and Service Records with no signal.
   Scope is this folder. guides/ and sds/ have their own workers (the more specific scope wins) and are left alone here.
   What it saves:
   1. lpc-app-<VERSION>: the four pages, lpc-config.js, lpc-clients.js, lpc-sw-banner.js, the manifest, the Service Records data files and the logos.
      Pages, scripts, data and the manifest are network first with a 4 second wait: the fresh copy is saved on every good answer,
      and the saved copy is used when the network is slow or gone. Logos are saved copy first.
   2. lpc-3p-v1: the pinned login library (jsDelivr, saved with its integrity check) and the Google Fonts files. Saved copy first.
   Never touched, these go straight through: Supabase (login, data, files, live updates), Google Maps / Places / Routes, Open-Meteo,
   OpenStreetMap, Anthropic, anything that isn't a plain GET, anything with a Range header, and every other page on the site.
   A new version never takes over mid-job. A new worker waits, each page shows "A new version is ready. Tap to update."
   (lpc-sw-banner.js), and only that tap sends SKIP, the one place skipWaiting() is called. Nothing reloads by itself.
   Changed a page, script or data file: nothing to do, network first picks it up. Changed this file, a logo or the list of
   what is saved: bump VERSION.
   KILL SWITCH: put ?nosw=1 on the end of any page address (home.html?nosw=1). The page unregisters this worker, deletes the lpc- caches,
   and reloads with it switched off on that device. ?nosw=0 switches it back on. More in tools/offline-notes.md. */
"use strict";

const VERSION = "2026-11-04b";
const APP = "lpc-app-" + VERSION;
const THIRD = "lpc-3p-v1";
const WAIT_MS = 4000;
const BASE = new URL("./", self.location.href);                       // the lighthouse-inspection/ folder
const ROOT = BASE.pathname;

// The four pages. A visit to the folder itself (no file name) is Inspection Capture, so it is saved as index.html.
const PAGES = ["home.html", "index.html", "calendar.html", "records.html"];
// The pages cannot open without these. If one fails to save, this worker does not install (the old one keeps working).
const CORE = PAGES.concat(["lpc-config.js", "lpc-clients.js", "lpc-sw-banner.js"]);
// Saved on a best-effort basis: a failure here never stops the install, and they are saved again on the next visit with signal.
// (The three jobbook-data files are the ones Service Records loads. The private Job Book data is never served by this site.)
// start.html is the "How to use these tools" page linked from Home (saved best-effort, and opened network first like the four pages, see the fetch handler).
const EXTRA = ["home.webmanifest", "start.html", "jobbook-data/settings.json", "jobbook-data/chemicals.json", "jobbook-data/services.json"];
const OWN = CORE.concat(EXTRA);
const IMAGES = ["logo/lpc-badge.png", "logo/logo-horizontal.svg", "logo/logo-stacked.svg", "logo/icon-32.png",
  "logo/icon-home-180.png", "logo/icon-calendar-180.png", "logo/icon-records-180.png", "logo/icon-capture-180.png"];
// The login library, pinned. Saved with the same integrity hash the pages use, so it still passes their integrity and crossorigin attributes.
const SB_URL = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.117.2/dist/umd/supabase.min.js";
const SB_HASH = "sha384-WgXwGL6fUsYJWNaKJgVbrJKGRQwc1vieh2oy4kw9nXqpNDz3tdSsqEYUgeHD/NuF";
// The font stylesheets, written exactly as the pages write them (Capture asks for fewer weights than the other three).
const FONT_CSS = [
  "https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,600;1,6..72,400&family=Public+Sans:wght@400;500;600;700&family=Spline+Sans+Mono:wght@400;500;600&display=swap",
  "https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,500;6..72,600&family=Public+Sans:wght@400;500;600;700&family=Spline+Sans+Mono:wght@500;600&display=swap"
];

self.addEventListener("install", function (e) {
  e.waitUntil((async function () {
    const app = await caches.open(APP);
    const third = await caches.open(THIRD);
    const jobs = OWN.map(function (rel) { return saveOwn(app, rel); })
      .concat(IMAGES.map(function (rel) { return saveOwn(app, rel); }))
      .concat([saveThird(third, SB_URL, SB_HASH)])
      .concat(FONT_CSS.map(function (u) { return saveFonts(third, u); }));
    const done = await Promise.allSettled(jobs);
    // OWN starts with CORE, so the first CORE.length answers are the ones that must have worked.
    for (let i = 0; i < CORE.length; i++) if (done[i].status === "rejected") throw done[i].reason;
    // No skipWaiting() here: a new version waits until the person taps the banner (see the message handler).
  })());
});

self.addEventListener("activate", function (e) {
  e.waitUntil((async function () {
    const names = await caches.keys();
    await Promise.all(names.map(function (k) {
      return (k.indexOf("lpc-app-") === 0 && k !== APP) ? caches.delete(k) : null;
    }));
    // No clients.claim(): pages already open keep the worker they started with. A new page picks this one up on its next load.
  })());
});

self.addEventListener("message", function (e) {
  if (e.data && e.data.type === "SKIP") self.skipWaiting();
});

self.addEventListener("fetch", function (e) {
  const req = e.request;
  if (req.method !== "GET" || req.headers.has("range")) return;
  if (req.cache === "only-if-cached" && req.mode !== "same-origin") return;
  const url = new URL(req.url);
  if (isLive(url)) return;

  if (url.origin !== self.location.origin) {
    if (url.href === SB_URL) e.respondWith(thirdFirst(e, req, true));
    else if (url.hostname === "fonts.googleapis.com" && url.pathname === "/css2") e.respondWith(thirdFirst(e, req, false));
    else if (url.hostname === "fonts.gstatic.com") e.respondWith(thirdFirst(e, req, false));
    return;
  }
  if (url.pathname.indexOf(ROOT) !== 0) return;
  let rel = url.pathname.slice(ROOT.length);
  if (rel === "") rel = "index.html";
  const key = new URL(rel, BASE).href;                                // no query string

  if (req.mode === "navigate") {
    if (PAGES.indexOf(rel) < 0 && rel !== "start.html") return;       // Job Book, proposals, guides/, sds/ and the rest: untouched
    if (url.searchParams.get("nosw") === "1") { e.waitUntil(switchOff()); return; }   // kill switch: straight to the network
    e.respondWith(netFirst(e, req, key, true));
  } else if (IMAGES.indexOf(rel) >= 0) {
    e.respondWith(imageFirst(e, req, key));
  } else if (OWN.indexOf(rel) >= 0) {
    e.respondWith(netFirst(e, req, key, false));
  }
});

// Live data and maps: never saved, never delayed. (The allow list above would skip them anyway; this keeps it that way if it grows.)
function isLive(u) {
  const h = u.hostname;
  return /(^|\.)(supabase\.co|supabase\.in|open-meteo\.com|openstreetmap\.org|anthropic\.com|google\.com)$/.test(h) ||
    (/(^|\.)googleapis\.com$/.test(h) && h !== "fonts.googleapis.com") || h === "maps.gstatic.com";
}

function good(res) { return res.ok && res.status === 200 && res.type === "basic" && !res.redirected; }

function keep(e, cache, key, res) {
  const job = cache.put(key, res).catch(function () {});
  try { e.waitUntil(job); } catch (err) { /* worker already winding down, the put still runs */ }
}

async function saveOwn(cache, rel) {
  const abs = new URL(rel, BASE).href;
  const res = await fetch(new Request(abs, { cache: "reload" }));
  if (!(res.ok && res.status === 200)) throw new Error(rel + " " + res.status);
  await cache.put(abs, res);
}

// A real (not opaque) answer is saved, so it can be checked against the page's integrity hash and read by the font code.
async function saveThird(cache, url, integrity) {
  const init = { mode: "cors", credentials: "omit" };
  if (integrity) init.integrity = integrity;
  const res = await fetch(url, init);
  if (!(res.ok && res.status === 200)) throw new Error(url + " " + res.status);
  await cache.put(url, res.clone());
  return res;
}

// The stylesheet, then the Latin font files it points at (what the pages' text uses), so the first offline open looks right.
async function saveFonts(cache, url) {
  const res = await saveThird(cache, url);
  const css = await res.clone().text();
  const files = [];
  css.replace(/\/\*\s*latin\s*\*\/\s*@font-face\s*\{[^}]*?url\(([^)\s]+)\)/g, function (m, u) { files.push(u.replace(/^['"]|['"]$/g, "")); return m; });
  await Promise.allSettled(files.map(function (f) { return saveThird(cache, f); }));
}

// Kill switch, worker side: forget everything. (The page does the same and reloads; this works even if the page's own script is the problem.)
async function switchOff() {
  try { await self.registration.unregister(); } catch (err) { /* the page's script does it too */ }
  const names = await caches.keys();
  await Promise.all(names.map(function (k) { return k.indexOf("lpc-") === 0 ? caches.delete(k) : null; }));
}

/* Pinned CDN file, fonts: saved copy first, network on a miss. strict = only a readable (cors) 200 is saved. */
async function thirdFirst(e, req, strict) {
  const cache = await caches.open(THIRD);
  const hit = await cache.match(req.url, { ignoreVary: true });
  if (hit) return hit;
  const res = await fetch(req);
  if (strict ? (res.ok && res.status === 200 && res.type === "cors") : (res.type === "opaque" || (res.ok && res.status === 200))) keep(e, cache, req.url, res.clone());
  return res;
}

/* Logos: saved copy first, network on a miss. */
async function imageFirst(e, req, key) {
  const cache = await caches.open(APP);
  const hit = await cache.match(key, { ignoreVary: true });
  if (hit) return hit;
  const res = await fetch(req);
  if (good(res)) keep(e, cache, key, res.clone());
  return res;
}

/* Pages, scripts, data, manifest: network first with a 4 second wait. */
async function netFirst(e, req, key, isNav) {
  const cache = await caches.open(APP);
  const saved = function () { return cache.match(key, { ignoreVary: true }); };
  const net = (isNav ? fetch(req) : fetch(req, { cache: "no-cache" })).then(function (res) {
    if (good(res)) keep(e, cache, key, res.clone());
    return res;
  });
  net.catch(function () {});                                          // quiet if it fails after we've moved on
  let timer;
  try {
    const first = await Promise.race([net, new Promise(function (r) { timer = setTimeout(function () { r(null); }, WAIT_MS); })]);
    clearTimeout(timer);
    if (first) {
      if (first.ok || first.type === "opaqueredirect") return first;
      return (await saved()) || first;                                // server error: prefer the saved copy
    }
    const c = await saved();                                          // 4 seconds up, network still going
    if (c) return c;
    return await net;                                                 // nothing saved yet, so keep waiting
  } catch (err) {
    clearTimeout(timer);
    const c = await saved();
    if (c) return c;
    return Response.error();
  }
}
