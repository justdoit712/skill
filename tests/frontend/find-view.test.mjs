import test from "node:test";
import assert from "node:assert/strict";
import { renderFindView } from "../../public/js/find-view.js";

class Element {
  constructor() { this.children = []; this.html = ""; }
  set innerHTML(value) { this.html = value; this.children = []; }
  get innerHTML() { return this.html; }
  appendChild(child) { this.children.push(child); }
  output() { return this.html + this.children.map(child => child.output()).join(""); }
}

function render(report) {
  const previous = globalThis.document;
  globalThis.document = { createElement: () => new Element() };
  try {
    const container = new Element();
    renderFindView(container, report);
    return container.output();
  } finally { globalThis.document = previous; }
}

const report = { schema_version: "1.0.0", topic: "需求", status: "completed", shortlist: [], alternatives: [] };

test("model text is escaped and unsafe card links are not executable", () => {
  const html = render({
    ...report,
    topic: '<img src=x onerror="bad()">',
    shortlist: [{
      candidate: { name: "<script>bad()</script>", url: "javascript:bad()" },
      evaluation: { match: "strong", summary_zh: "<b>raw</b>" }
    }]
  });
  assert.doesNotMatch(html, /<script>|<img|href="javascript:/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /href="#"/);
});
