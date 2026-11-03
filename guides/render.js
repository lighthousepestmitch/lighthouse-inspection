/* Field Guides renderer. Turns a guide's JSON into an HTML string. Pure functions only (no DOM), so the page and the
   node test (guides/tests/check.js) run exactly the same code. Everything from the JSON is escaped before it goes in. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.LPCRender = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const TYPES = ["text", "checklist", "callout", "table", "qa"];
  const KINDS = ["legal", "never", "note"];
  const KIND_LABEL = { legal: "Legal", never: "Never", note: "Note" };
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const QA_TITLE = "What to say if the customer asks";
  const DEFAULT_RESET_NOTE = "Ticks are saved on this iPad. Reset them for each new job.";
  const CONFIRM_RE = /\[Mitch to confirm[^\]]*\]/g;

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* Escaped text with three small extras: [Mitch to confirm ...] becomes a visible marker, the business phone and email
     become tap links, and a newline becomes a line break. */
  function inline(s) {
    let h = esc(s);
    h = h.replace(CONFIRM_RE, function (m) { return '<span class="confirm">' + m + "</span>"; });
    h = h.replace(/0432 181 689/g, '<a href="tel:0432181689">0432 181 689</a>');
    h = h.replace(/mitch@lighthousepest\.com\.au/g, '<a href="mailto:mitch@lighthousepest.com.au">mitch@lighthousepest.com.au</a>');
    return h.replace(/\n/g, "<br>");
  }

  /* "2026-11-04" -> "4 Nov 2026". Anything else comes back as it was. */
  function fmtDate(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || ""));
    if (!m) return String(s || "");
    const mo = +m[2];
    if (mo < 1 || mo > 12) return String(s);
    return +m[3] + " " + MON[mo - 1] + " " + m[1];
  }

  const paras = (v) => (Array.isArray(v) ? v : v == null ? [] : [v]).map(String).filter(Boolean);
  const item = (x) => (typeof x === "string" ? { text: x, detail: "" } : { text: String((x && x.text) || ""), detail: String((x && x.detail) || "") });
  const bkey = (s, i) => (s && typeof s.id === "string" && /^[A-Za-z0-9_-]+$/.test(s.id) ? s.id : "b" + i);

  /* ---------- checks (also used by the node test) ---------- */
  function validateGuide(g) {
    const bad = [];
    const say = (m) => bad.push(m);
    if (!g || typeof g !== "object") return ["guide is not an object"];
    if (!/^[a-z0-9-]+$/.test(g.id || "")) say("id must be lower case letters, numbers and hyphens");
    if (!g.title || typeof g.title !== "string") say("missing title");
    if (!/^\d{4}-\d{2}-\d{2}$/.test(g.updated || "")) say("updated must be YYYY-MM-DD");
    if (!Array.isArray(g.sections) || !g.sections.length) { say("sections must be a non-empty list"); return bad; }
    const seen = {};
    g.sections.forEach(function (s, i) {
      const at = "section " + (i + 1);
      if (!s || typeof s !== "object") { say(at + " is not an object"); return; }
      if (!TYPES.includes(s.type)) { say(at + ": unknown type " + JSON.stringify(s.type)); return; }
      if (s.id != null) {
        if (!/^[A-Za-z0-9_-]+$/.test(String(s.id))) say(at + ": id has odd characters");
        if (seen[s.id]) say(at + ": duplicate id " + s.id);
        seen[s.id] = true;
      }
      const isStr = (v) => typeof v === "string" && v.trim() !== "";
      const itemsOk = (arr) => Array.isArray(arr) && arr.length > 0 && arr.every(function (x) { return isStr(x) || (x && isStr(x.text)); });
      if (s.type === "text" && !paras(s.body).length) say(at + ": text needs body");
      if (s.type === "checklist" && !itemsOk(s.items)) say(at + ": checklist needs items (strings or {text, detail})");
      if (s.type === "callout") {
        if (!KINDS.includes(s.kind)) say(at + ": callout kind must be legal, never or note");
        if (!paras(s.body).length && !(Array.isArray(s.items) && s.items.length)) say(at + ": callout needs body or items");
        if (s.items != null && !itemsOk(s.items)) say(at + ": callout items must be strings or {text, detail}");
      }
      if (s.type === "table") {
        if (!Array.isArray(s.rows) || !s.rows.length) say(at + ": table needs rows");
        else {
          const n = s.columns ? s.columns.length : s.rows[0].length;
          s.rows.forEach(function (r, ri) {
            if (!Array.isArray(r) || r.length !== n || !r.every(function (c) { return typeof c === "string"; })) say(at + ": table row " + (ri + 1) + " must be " + n + " text cells");
          });
        }
        if (s.columns != null && !(Array.isArray(s.columns) && s.columns.every(function (c) { return typeof c === "string"; }))) say(at + ": table columns must be text");
      }
      if (s.type === "qa") {
        if (!Array.isArray(s.items) || !s.items.length || !s.items.every(function (x) { return x && isStr(x.q) && isStr(x.a); })) say(at + ": qa needs items of {q, a}");
      }
    });
    return bad;
  }

  /* ---------- one block ---------- */
  function titleHtml(s) { return s.title ? '<h3 class="blk-title">' + inline(s.title) + "</h3>" : ""; }

  function renderText(s) {
    return titleHtml(s) + paras(s.body).map(function (p) { return "<p>" + inline(p) + "</p>"; }).join("");
  }

  function renderChecklist(s, bk, ticks, count) {
    const lis = (Array.isArray(s.items) ? s.items : []).map(function (raw, i) {
      const it = item(raw), k = bk + "." + i, on = ticks.has(k);
      count.total++; if (on) count.checked++;
      return '<li' + (on ? ' class="done"' : "") + '><label class="tick"><input type="checkbox" data-k="' + esc(k) + '"' + (on ? " checked" : "") +
        '><span class="box" aria-hidden="true"></span><span class="tx"><span class="t">' + inline(it.text) + "</span>" +
        (it.detail ? '<span class="d">' + inline(it.detail) + "</span>" : "") + "</span></label></li>";
    }).join("");
    const head = (s.title || s.note)
      ? '<div class="ticks-t">' + titleHtml(s) + (s.note ? '<p class="blk-note">' + inline(s.note) + "</p>" : "") + "</div>" : "";
    return '<div class="cl">' + head + '<ul class="ticks">' + lis + "</ul></div>";
  }

  function renderCallout(s) {
    const kind = KINDS.includes(s.kind) ? s.kind : "note";
    const label = '<span class="co-label">' + KIND_LABEL[kind] + "</span>";
    const head = '<div class="co-h">' + label + (s.title ? '<span class="co-t">' + inline(s.title) + "</span>" : "") + "</div>";
    const body = paras(s.body).map(function (p) { return "<p>" + inline(p) + "</p>"; }).join("");
    const list = Array.isArray(s.items) && s.items.length
      ? '<ul class="co-list">' + s.items.map(function (raw) {
        const it = item(raw);
        return '<li><span class="t">' + inline(it.text) + "</span>" + (it.detail ? '<span class="d">' + inline(it.detail) + "</span>" : "") + "</li>";
      }).join("") + "</ul>" : "";
    return '<aside class="callout k-' + kind + '">' + head + body + list + "</aside>";
  }

  function renderTable(s) {
    const cols = Array.isArray(s.columns) ? s.columns : null;
    const rows = Array.isArray(s.rows) ? s.rows : [];
    const bold = s.firstColBold !== false;
    const cls = "tbl" + (cols && cols.length >= 3 ? " lbl" : "");
    const thead = cols ? "<thead><tr>" + cols.map(function (c) { return '<th scope="col">' + inline(c) + "</th>"; }).join("") + "</tr></thead>" : "";
    const tbody = "<tbody>" + rows.map(function (r) {
      return "<tr>" + (Array.isArray(r) ? r : []).map(function (c, ci) {
        const lab = cols && cols[ci] ? ' data-label="' + esc(cols[ci]) + '"' : "";
        return '<td class="' + (ci === 0 && bold ? "c0" : "cx") + '"' + lab + ">" + inline(c) + "</td>";
      }).join("") + "</tr>";
    }).join("") + "</tbody>";
    return '<div class="tbl-wrap">' + titleHtml(s) + '<table class="' + cls + '">' + thead + tbody + "</table></div>";
  }

  function renderQa(s) {
    const title = s.title || QA_TITLE;
    const items = (Array.isArray(s.items) ? s.items : []).map(function (x) {
      // markers sit after the closing quote so they never read as something to say out loud
      const chips = []; const clean = String(x.a || "").replace(CONFIRM_RE, function (m) { chips.push(m); return ""; }).trim();
      return '<div class="qa-i"><div class="qa-q"><span class="who">Customer</span>“' + inline(x.q) + "”</div>" +
        '<div class="qa-a"><span class="who">You</span>“' + inline(clean) + "”" +
        chips.map(function (c) { return ' <span class="confirm">' + esc(c) + "</span>"; }).join("") + "</div></div>";
    }).join("");
    return '<div class="qa-box"><div class="qa-h">' + inline(title) + "</div>" + items + "</div>";
  }

  /* ---------- a whole guide ---------- */
  function renderGuide(g, opts) {
    opts = opts || {};
    const ticks = opts.ticks || new Set();
    const count = { total: 0, checked: 0 };
    const jumps = [];
    const secs = Array.isArray(g.sections) ? g.sections : [];
    const body = secs.map(function (s, i) {
      const bk = bkey(s, i), id = "sec-" + bk;
      let inner;
      if (!s || !TYPES.includes(s.type)) inner = '<div class="callout k-note"><p>This part of the guide could not be shown (unknown type).</p></div>';
      else if (s.type === "text") inner = renderText(s);
      else if (s.type === "checklist") inner = renderChecklist(s, bk, ticks, count);
      else if (s.type === "callout") inner = renderCallout(s);
      else if (s.type === "table") inner = renderTable(s);
      else inner = renderQa(s);
      let head = "";
      if (s && s.heading) {
        head = '<h2 class="sec-h">' + inline(s.heading) + "</h2>";
        jumps.push({ id: id, label: String(s.short || s.heading) });
      }
      return '<section class="blk blk-' + esc(s && s.type) + '" id="' + esc(id) + '">' + head + inner + "</section>";
    }).join("");

    const note = g.resetNote || DEFAULT_RESET_NOTE;
    const hasTicks = count.total > 0;
    const resetBtn = '<button type="button" class="btn" data-reset>Reset ticks</button>';
    const intro = '<div class="g-intro"><h1>' + inline(g.title) + "</h1>" +
      (g.subtitle ? '<p class="g-sub">' + inline(g.subtitle) + "</p>" : "") +
      '<p class="g-meta">Updated ' + esc(fmtDate(g.updated)) + "</p>" +
      (hasTicks
        ? '<div class="tickbar noprint"><p class="tickfine">' + inline(note) + '</p><div class="tb-row"><span class="prog-txt" data-prog-txt>' +
          count.checked + " of " + count.total + " ticked</span>" + resetBtn + "</div></div>"
        : "") + "</div>";
    const end = (hasTicks ? '<div class="g-end noprint"><p class="tickfine">' + inline(note) + "</p>" + resetBtn + "</div>" : "") +
      '<p class="g-foot">Lighthouse Pest Control | ' + inline("0432 181 689") + " | " + inline("mitch@lighthousepest.com.au") + "</p>";
    return { html: intro + body + end, jumps: jumps, total: count.total, checked: count.checked, sections: secs.length };
  }

  /* ---------- home list ---------- */
  function renderCards(entries, opts) {
    opts = opts || {};
    const have = opts.available || function () { return true; };
    const groups = [];
    entries.forEach(function (e) {
      const name = e.group || "Guides";
      let grp = groups.find(function (x) { return x.name === name; });
      if (!grp) { grp = { name: name, list: [] }; groups.push(grp); }
      grp.list.push(e);
    });
    const chev = '<svg class="chev" viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>';
    return groups.map(function (grp) {
      return '<section class="grp"><h2 class="grp-h">' + esc(grp.name) + '</h2><div class="cards">' + grp.list.map(function (e) {
        return '<a class="card" href="#' + esc(e.id) + '"><span class="card-b"><span class="card-t">' + esc(e.title) +
          (have(e.id) ? "" : '<span class="pill">Not saved yet</span>') + "</span>" +
          (e.summary ? '<span class="card-s">' + esc(e.summary) + "</span>" : "") +
          '<span class="card-m">Updated ' + esc(fmtDate(e.updated)) + "</span></span>" + chev + "</a>";
      }).join("") + "</div></section>";
    }).join("");
  }

  /* all the words in a guide, lower case, for the search box */
  function searchText(g, entry) {
    const out = [];
    const add = function (v) { if (v == null) return; if (Array.isArray(v)) v.forEach(add); else if (typeof v === "object") Object.keys(v).forEach(function (k) { add(v[k]); }); else out.push(String(v)); };
    if (entry) add([entry.title, entry.summary, entry.group, entry.tags]);
    if (g) {
      add([g.title, g.subtitle]);
      (g.sections || []).forEach(function (s) {
        if (!s) return;
        add([s.heading, s.title, s.note, s.body, s.items, s.columns, s.rows]);
      });
    }
    return out.join(" ").replace(/\s+/g, " ").toLowerCase();
  }

  return { esc: esc, inline: inline, fmtDate: fmtDate, validateGuide: validateGuide, renderGuide: renderGuide, renderCards: renderCards, searchText: searchText, TYPES: TYPES, KINDS: KINDS, DEFAULT_RESET_NOTE: DEFAULT_RESET_NOTE };
});
