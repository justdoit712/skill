import test from "node:test";
import assert from "node:assert/strict";
import { qualityBlock } from "../../public/js/catalog-view.js";

test("old entries are not labelled as deeply reviewed", () => {
  assert.equal(qualityBlock({}), "");
});

test("quality rationale and review disagreement are visible and escaped", () => {
  const html = qualityBlock({ quality_summary: {
    review_status: "disagreed", review_note: "需要复核",
    checks: { practical_value: { value: "pass", evidence: "<script>bad()</script>" } },
    review_checks: { verification: { value: "unknown", evidence: "缺少验收条件" } }
  } });
  assert.ok(html.includes("两轮评估有分歧"));
  assert.ok(html.includes("缺少验收条件"));
  assert.ok(html.includes("&lt;script&gt;"));
  assert.ok(!html.includes("<script>"));
});

import { renderCatalogList } from "../../public/js/catalog-view.js";
import { initConfirmModal } from "../../public/js/modals.js";
import { createOverridesState } from "../../public/js/catalog-state.js";

class Element {
  constructor() {
    this.children = [];
    this.html = "";
    this.listeners = {};
    this.hidden = true;
    this.textContent = "";
  }
  set innerHTML(value) { this.html = value; this.children = []; }
  get innerHTML() { return this.html; }
  appendChild(child) { this.children.push(child); }
  addEventListener(event, fn) {
    if (!this.listeners[event]) this.listeners[event] = [];
    this.listeners[event].push(fn);
  }
  trigger(event, data = {}) {
    (this.listeners[event] || []).forEach(fn => fn(data));
  }
  output() { return this.html + this.children.map(child => child.output()).join(""); }
}

test("catalog-view: action buttons partitioned correctly across tabs", () => {
  const prevDoc = globalThis.document;
  globalThis.document = { createElement: () => new Element() };
  try {
    const entry = { skill_id: "test:skill", name: "test-skill", url: "https://example.com" };
    const overridesState = createOverridesState();

    // 1. Manual tab: ONLY manual has owned button
    const containerManual = new Element();
    renderCatalogList(containerManual, [entry], overridesState, "manual");
    const htmlManual = containerManual.output();
    assert.ok(htmlManual.includes('class="btn-action btn-owned"'));

    // 2. Candidate tab: DOES NOT have owned button
    const containerCandidate = new Element();
    renderCatalogList(containerCandidate, [entry], overridesState, "candidate");
    const htmlCandidate = containerCandidate.output();
    assert.ok(!htmlCandidate.includes('class="btn-action btn-owned"'));

    // 3. Recommended tab: DOES NOT have owned button
    const containerRec = new Element();
    renderCatalogList(containerRec, [entry], overridesState, "recommended");
    const htmlRec = containerRec.output();
    assert.ok(!htmlRec.includes('class="btn-action btn-owned"'));
  } finally {
    globalThis.document = prevDoc;
  }
});

test("modals: initConfirmModal handles open, cancel, and confirm flow", () => {
  const prevDoc = globalThis.document;
  const docListeners = {};
  globalThis.document = {
    addEventListener: (event, fn) => {
      if (!docListeners[event]) docListeners[event] = [];
      docListeners[event].push(fn);
    }
  };

  try {
    const elements = {
      confirmModal: new Element(),
      confirmModalSkillName: new Element(),
      btnCancelConfirm: new Element(),
      btnSubmitConfirm: new Element(),
      btnCloseConfirmModal: new Element()
    };

    const controller = initConfirmModal(elements);
    let confirmed = false;

    // Test open
    controller.open("my-awesome-skill", () => {
      confirmed = true;
    });
    assert.equal(elements.confirmModal.hidden, false);
    assert.equal(elements.confirmModalSkillName.textContent, "my-awesome-skill");

    // Test cancel
    elements.btnCancelConfirm.trigger("click");
    assert.equal(elements.confirmModal.hidden, true);
    assert.equal(confirmed, false);

    // Test open again and confirm
    controller.open("my-awesome-skill", () => {
      confirmed = true;
    });
    assert.equal(elements.confirmModal.hidden, false);
    elements.btnSubmitConfirm.trigger("click");
    assert.equal(elements.confirmModal.hidden, true);
    assert.equal(confirmed, true);
  } finally {
    globalThis.document = prevDoc;
  }
});
