import test from "node:test";
import assert from "node:assert/strict";
import { qualityBlock } from "../../public/js/catalog-view.js";

test("quality rationale and review disagreement are visible and escaped", () => {
  const entry = {
    quality_summary: {
      review_status: "disagreed",
      review_note: "复核分歧",
      blocking_reasons: ["阻断原因"],
      checks: { practical_value: { value: "pass", evidence: "证据 1" } },
      review_checks: { practical_value: { value: "fail", evidence: "复核证据 <unsafe>" } }
    }
  };
  const html = qualityBlock(entry);
  assert.match(html, /两轮评估有分歧/);
  assert.match(html, /复核分歧/);
  assert.match(html, /阻断原因/);
  assert.match(html, /&lt;unsafe&gt;/);
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
    this.parent = null;
  }
  set innerHTML(value) { this.html = value; this.children = []; }
  get innerHTML() { return this.html; }
  appendChild(child) {
    if (child) {
      child.parent = this;
      this.children.push(child);
    }
  }
  remove() {
    if (this.parent && this.parent.children) {
      const idx = this.parent.children.indexOf(this);
      if (idx !== -1) this.parent.children.splice(idx, 1);
    }
  }
  addEventListener(event, fn) {
    if (!this.listeners[event]) this.listeners[event] = [];
    this.listeners[event].push(fn);
  }
  trigger(event, data = {}) {
    (this.listeners[event] || []).forEach(fn => fn(data));
  }
  output() { return (this.textContent || "") + this.html + this.children.map(child => child.output()).join(""); }
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

test("catalog-view: progressive rendering paginates entries and loads more", () => {
  const prevDoc = globalThis.document;
  globalThis.document = { createElement: () => new Element() };
  try {
    const entries = Array.from({ length: 30 }, (_, i) => ({
      skill_id: `skill:${i}`,
      name: `Skill ${i}`,
      url: `https://example.com/${i}`
    }));
    const overridesState = createOverridesState();
    const container = new Element();

    let batchNotification = null;
    const totalMatching = renderCatalogList(
      container,
      entries,
      overridesState,
      "recommended",
      {},
      {
        pageSize: 10,
        onBatchRendered: (rendered, total) => {
          batchNotification = { rendered, total };
        }
      }
    );

    assert.equal(totalMatching, 30);
    assert.equal(batchNotification.rendered, 10);
    assert.equal(batchNotification.total, 30);
    assert.equal(container.children.length, 11);
    assert.ok(container.output().includes("加载更多条目（还剩 20 条）"));

    // Find and trigger load more button
    const loadMoreLi = container.children[container.children.length - 1];
    const btn = loadMoreLi.children[0];
    btn.trigger("click");

    assert.equal(batchNotification.rendered, 20);
    assert.ok(container.output().includes("加载更多条目（还剩 10 条）"));
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
