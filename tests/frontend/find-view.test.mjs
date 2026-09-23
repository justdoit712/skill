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

test("unknown schema is rejected before interpreting result fields", () => {
  assert.match(render({ ...report, schema_version: "2.0" }), /报告版本不兼容/);
});

test("legacy report remains readable and missing usage is unknown", () => {
  const legacy = { ...report };
  delete legacy.schema_version;
  assert.match(render(legacy), /未知/);
  assert.doesNotMatch(render(legacy), /版本不兼容/);
});

test("partial coverage and unknown usage are visible", () => {
  const html = render({ ...report, status: "stopped", stop_reason: "usage_unknown",
    coverage_incomplete: true, usage: { total_tokens: 12, unknown_usage_requests: 1 } });
  assert.match(html, /覆盖不完整/);
  assert.match(html, /仅为已知用量/);
});

test("model text is escaped and unsafe card links are not executable", () => {
  const html = render({ ...report, topic: '<img src=x onerror="bad()">', shortlist: [{
    candidate: { name: "<script>bad()</script>", url: "javascript:bad()" },
    evaluation: { match: "strong", summary_zh: "<b>raw</b>" }
  }] });
  assert.doesNotMatch(html, /<script>|<img|href="javascript:/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /href="#"/);
});

test("running, error, interruption, and empty completion have distinct output", () => {
  assert.match(render({ ...report, status: "running" }), /尚未完成/);
  assert.match(render({ ...report, status: "error", stop_reason: "material_failed" }), /异常中止/);
  assert.match(render({ ...report, status: "interrupted", stop_reason: "interrupted" }), /中断/);
  assert.doesNotMatch(render(report), /异常中止|尚未完成/);
});
