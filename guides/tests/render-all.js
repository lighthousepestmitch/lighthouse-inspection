/* Renders every guide with render.js and prints one line of JSON for check.py to judge.
   Runs in Node or in macOS's built-in JavaScriptCore shell. Run it from the guides/ folder:
   /System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc tests/render-all.js
   (or: node tests/render-all.js). It needs no browser and no server. */
(function () {
  "use strict";
  var isNode = typeof require === "function" && typeof process !== "undefined";
  var slurp = isNode ? function (p) { return require("fs").readFileSync(p, "utf8"); } : read;
  var out = isNode ? function (s) { console.log(s); } : print;
  var R;
  if (isNode) R = require(process.cwd() + "/render.js"); else { load("render.js"); R = LPCRender; }

  var idx = JSON.parse(slurp("data/index.json"));
  var report = { guides: [], cards: {}, escape: "", problems: [] };

  idx.guides.forEach(function (e) {
    var raw = slurp("data/" + e.id + ".json");
    var g = JSON.parse(raw);
    var bad = R.validateGuide(g);
    var r = R.renderGuide(g, {});
    var blocks = (r.html.match(/class="blk blk-/g) || []).length;
    var heads = g.sections.filter(function (s) { return s.heading; });
    var missingHeads = heads.filter(function (s) { return r.html.indexOf(R.inline(s.heading)) < 0; }).map(function (s) { return s.heading; });
    var markers = (raw.match(/\[Mitch to confirm[^\]]*\]/g) || []).length;
    var chips = (r.html.match(/class="confirm"/g) || []).length;
    var itemCount = 0;
    g.sections.forEach(function (s) { if (s.type === "checklist") itemCount += s.items.length; });
    // tick state round trip: tick the first item of the first checklist and render again
    var first = null;
    g.sections.some(function (s, i) { if (s.type === "checklist") { first = (/^[A-Za-z0-9_-]+$/.test(s.id || "") ? s.id : "b" + i) + ".0"; return true; } return false; });
    var t = first ? R.renderGuide(g, { ticks: new Set([first]) }) : r;
    report.guides.push({
      id: e.id, sections: g.sections.length, types: g.sections.map(function (s) { return s.type; }),
      problems: bad, blocksRendered: blocks, headings: heads.length, missingHeadings: missingHeads, jumps: r.jumps.length,
      markers: markers, chips: chips, tickTotal: r.total, checklistItems: itemCount,
      tickedAfter: t.checked, tickInputsChecked: (t.html.match(/ checked>/g) || []).length, htmlLength: r.html.length,
      search: R.searchText(g, e).length
    });
  });

  var cards = R.renderCards(idx.guides, { available: function () { return true; } });
  idx.guides.forEach(function (e) {
    report.cards[e.id] = cards.indexOf('href="#' + e.id + '"') >= 0 && cards.indexOf(R.esc(e.title)) >= 0;
  });
  report.escape = R.inline('<b>&"x"</b> [Mitch to confirm]');
  report.fmt = R.fmtDate("2026-11-04");
  report.searchHits = {};
  ["subfloor", "ppe", "spider", "weep holes", "zzzz"].forEach(function (q) {
    report.searchHits[q] = idx.guides.filter(function (e) {
      var g = JSON.parse(slurp("data/" + e.id + ".json"));
      var hay = R.searchText(g, e);
      return q.split(" ").every(function (tok) { return hay.indexOf(tok) >= 0; });
    }).map(function (e) { return e.id; });
  });
  out(JSON.stringify(report));
})();
