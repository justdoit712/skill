/**
 * 前端弹窗控制器与交互行为单元测试 (node --test)。
 * 模拟 DOM / 剪贴板 / 下载 / 窗口打开等浏览器 API，覆盖页签、动作、文件名、剪贴板失败与防御。
 */

import test from "node:test";
import assert from "node:assert/strict";

import {
  initSyncModal,
  updateSyncBar
} from "../../public/js/modals.js";

import {
  createOverridesState,
  togglePick,
  snoozeSkill
} from "../../public/js/catalog-state.js";

import {
  createOwnedState,
  markOwned,
  generateOwnedPatch
} from "../../public/js/owned-state.js";

class MockClassList {
  constructor() {
    this._classes = new Set();
  }
  add(...cls) { cls.forEach(c => this._classes.add(c)); }
  remove(...cls) { cls.forEach(c => this._classes.delete(c)); }
  toggle(cls, force) {
    if (force === true) { this._classes.add(cls); return true; }
    if (force === false) { this._classes.delete(cls); return false; }
    if (this._classes.has(cls)) { this._classes.delete(cls); return false; }
    this._classes.add(cls); return true;
  }
  contains(cls) { return this._classes.has(cls); }
}

function createMockElement(tagName = "div", id = "") {
  return {
    tagName: tagName.toUpperCase(),
    id: id,
    style: {},
    classList: new MockClassList(),
    attributes: {},
    hidden: false,
    disabled: false,
    _textContent: "",
    _innerHTML: "",
    listeners: {},
    children: [],

    get textContent() { return this._textContent; },
    set textContent(val) { this._textContent = String(val); this._innerHTML = String(val); },

    get innerHTML() { return this._innerHTML; },
    set innerHTML(val) { this._innerHTML = String(val); this._textContent = String(val); },

    setAttribute(name, val) { this.attributes[name] = String(val); },
    getAttribute(name) { return this.attributes[name] || null; },
    removeAttribute(name) { delete this.attributes[name]; },

    addEventListener(event, fn) {
      if (!this.listeners[event]) this.listeners[event] = [];
      this.listeners[event].push(fn);
    },
    removeEventListener(event, fn) {
      if (!this.listeners[event]) return;
      this.listeners[event] = this.listeners[event].filter(f => f !== fn);
    },
    click() {
      if (this.disabled) return;
      const fns = this.listeners["click"] || [];
      fns.forEach(fn => fn({ target: this, preventDefault: () => {} }));
    },
    focus() {
      if (globalThis.document) {
        globalThis.document.activeElement = this;
      }
    },
    blur() {},
    appendChild(child) { this.children.push(child); return child; },
    removeChild(child) {
      this.children = this.children.filter(c => c !== child);
      return child;
    },
    querySelectorAll(selector) {
      return this.children.filter(Boolean);
    },
    querySelector(selector) {
      return this.children[0] || null;
    }
  };
}

function setupMockEnv() {
  const elements = {
    syncBar: createMockElement("aside", "sync-bar"),
    syncSummary: createMockElement("span", "sync-summary"),
    btnOpenSync: createMockElement("button", "btn-open-sync"),
    btnClearSync: createMockElement("button", "btn-clear-sync"),
    syncModal: createMockElement("div", "sync-modal"),
    btnCloseModal: createMockElement("button", "btn-close-modal"),
    modalTitle: createMockElement("h3", "modal-title"),
    modalDesc: createMockElement("p", "modal-desc"),
    modalCrossWarning: createMockElement("p", "modal-cross-warning"),
    tabModalOverrides: createMockElement("button", "tab-modal-overrides"),
    tabModalSnoozed: createMockElement("button", "tab-modal-snoozed"),
    tabModalOwned: createMockElement("button", "tab-modal-owned"),
    jsonPreview: createMockElement("pre", "json-preview"),
    jsonViewModeLabel: createMockElement("span", "json-view-mode-label"),
    jsonViewToggles: createMockElement("div", "json-view-toggles"),
    btnViewIncremental: createMockElement("button", "btn-view-incremental"),
    btnViewFull: createMockElement("button", "btn-view-full"),
    copyStatus: createMockElement("p", "copy-status"),
    btnCopyJson: createMockElement("button", "btn-copy-json"),
    btnDownloadJson: createMockElement("button", "btn-download-json"),
    btnGotoGithub: createMockElement("button", "btn-goto-github")
  };

  elements.syncModal.children = [
    elements.btnCloseModal,
    elements.tabModalOverrides,
    elements.tabModalSnoozed,
    elements.tabModalOwned,
    elements.btnViewIncremental,
    elements.btnViewFull,
    elements.btnCopyJson,
    elements.btnDownloadJson,
    elements.btnGotoGithub
  ];

  const downloads = [];
  const openedWindows = [];
  let clipboardText = null;
  let clipboardFail = false;

  // Mock global document
  globalThis.document = {
    activeElement: null,
    getElementById: id => elements[id] || null,
    createElement: tag => {
      const el = createMockElement(tag);
      if (tag === "a") {
        el.click = () => {
          downloads.push({ href: el.href, download: el.download });
        };
      }
      return el;
    },
    body: {
      appendChild: () => {},
      removeChild: () => {}
    }
  };

  // Mock Blob & URL
  globalThis.Blob = class {
    constructor(chunks, options) {
      this.content = chunks.join("");
      this.options = options;
    }
  };

  globalThis.URL = {
    createObjectURL: blob => "blob:mock-" + encodeURIComponent(blob.content.slice(0, 30)),
    revokeObjectURL: () => {}
  };

  // Mock navigator.clipboard
  globalThis.navigator = {
    clipboard: {
      writeText: text => {
        if (clipboardFail) {
          return Promise.reject(new Error("NotAllowedError: Permission denied"));
        }
        clipboardText = text;
        return Promise.resolve();
      }
    }
  };

  // Mock window.open
  globalThis.window = {
    open: (url, target, features) => {
      openedWindows.push({ url, target, features });
      return { closed: false };
    }
  };

  // Mock confirm
  globalThis.confirm = () => true;

  return {
    elements,
    downloads,
    openedWindows,
    getClipboard: () => clipboardText,
    setClipboardFail: fail => { clipboardFail = fail; }
  };
}

test("modals: 8.2.1 默认增量、切换完整、关闭再打开复位；仅 owned 变化、混合变化、零变化均正常", () => {
  const env = setupMockEnv();
  const overridesState = createOverridesState();
  const ownedState = createOwnedState();

  togglePick(overridesState, "my/skill", "recommended", "2026-09-29");

  const modal = initSyncModal(env.elements, overridesState, null, ownedState);

  // 1. 打开弹窗：默认增量视图，overrides 页签
  modal.openSyncModal();
  assert.equal(env.elements.syncModal.hidden, false);
  assert.equal(modal.getCurrentTab(), "overrides");
  assert.equal(modal.getCurrentViewMode(), "incremental");
  assert.ok(env.elements.modalTitle.textContent.includes("待同步变更"));
  assert.equal(env.elements.btnViewIncremental.getAttribute("aria-pressed"), "true");
  assert.equal(env.elements.btnViewFull.getAttribute("aria-pressed"), "false");

  // 2. 切换为完整配置
  env.elements.btnViewFull.click();
  assert.equal(modal.getCurrentViewMode(), "full");
  assert.ok(env.elements.modalTitle.textContent.includes("完整配置"));
  assert.equal(env.elements.btnViewFull.getAttribute("aria-pressed"), "true");

  // 3. 关闭弹窗后再打开，应重置为增量视图
  modal.closeSyncModal();
  assert.equal(env.elements.syncModal.hidden, true);
  modal.openSyncModal();
  assert.equal(modal.getCurrentViewMode(), "incremental");

  // 4. 仅 owned 变化时打开弹窗，自动选择 owned 页签，且隐藏双视图按钮
  const overridesEmpty = createOverridesState();
  const ownedOnly = createOwnedState();
  markOwned(ownedOnly, { skill_id: "owned/skill", name: "已收录" });

  const modalOwned = initSyncModal(env.elements, overridesEmpty, null, ownedOnly);
  modalOwned.openSyncModal();
  assert.equal(modalOwned.getCurrentTab(), "owned");
  assert.equal(env.elements.jsonViewToggles.style.display, "none");

  // 5. 零变化时打开弹窗：默认 overrides，显示无待同步变更提示
  const allEmptyOverrides = createOverridesState();
  const allEmptyOwned = createOwnedState();
  const modalEmpty = initSyncModal(env.elements, allEmptyOverrides, null, allEmptyOwned);
  modalEmpty.openSyncModal();
  assert.equal(modalEmpty.getCurrentTab(), "overrides");
  assert.ok(env.elements.modalDesc.textContent.includes("当前文件没有待同步变更"));
});

test("modals: 8.2.2 复制/下载内容与当前预览逐字一致，增量文件名有 changes-preview 标识", async () => {
  const env = setupMockEnv();
  const overridesState = createOverridesState();
  togglePick(overridesState, "my/skill", "recommended", "2026-09-29");

  const modal = initSyncModal(env.elements, overridesState, null, null);
  modal.openSyncModal();

  // 1. 增量视图下载
  env.elements.btnDownloadJson.click();
  assert.equal(env.downloads.length, 1);
  assert.equal(env.downloads[0].download, "overrides-changes-preview.json");

  // 2. 增量视图复制：内容与 preview 逐字一致
  env.elements.btnCopyJson.click();
  await new Promise(r => setTimeout(r, 10));
  assert.equal(env.getClipboard(), env.elements.jsonPreview.textContent);
  assert.ok(env.elements.copyStatus.textContent.includes("已成功复制"));

  // 3. 切换完整配置后下载与复制
  env.elements.btnViewFull.click();
  env.elements.btnDownloadJson.click();
  assert.equal(env.downloads[1].download, "overrides.json");

  env.elements.btnCopyJson.click();
  await new Promise(r => setTimeout(r, 10));
  assert.equal(env.getClipboard(), env.elements.jsonPreview.textContent);
});

test("modals: 8.2.3 增量视图的同步按钮只切完整视图；完整视图复制成功后才尝试打开对应文件编辑页", async () => {
  const env = setupMockEnv();
  const overridesState = createOverridesState();
  togglePick(overridesState, "tool/abc", "recommended", "2026-09-29");

  const modal = initSyncModal(env.elements, overridesState, null, null);
  modal.openSyncModal();
  assert.equal(modal.getCurrentViewMode(), "incremental");

  // 点击增量模式下的主按钮 -> 只切完整视图，不打开网页
  env.elements.btnGotoGithub.click();
  assert.equal(modal.getCurrentViewMode(), "full");
  assert.equal(env.openedWindows.length, 0);

  // 在完整视图下点击主按钮 -> 复制并尝试打开编辑页
  env.elements.btnGotoGithub.click();
  await new Promise(r => setTimeout(r, 10));
  assert.equal(env.openedWindows.length, 1);
  assert.ok(env.openedWindows[0].url.includes("config/governance/overrides.json"));
  assert.equal(env.getClipboard(), env.elements.jsonPreview.textContent);
});

test("modals: 8.2.4 覆盖剪贴板不存在、Promise 拒绝、弹窗拦截、复制中换页签，不能出现虚假成功提示", async () => {
  const env = setupMockEnv();
  const overridesState = createOverridesState();
  togglePick(overridesState, "tool/err", "recommended", "2026-09-29");

  const modal = initSyncModal(env.elements, overridesState, null, null);
  modal.openSyncModal();

  // 1. 剪贴板 Promise 拒绝
  env.setClipboardFail(true);
  env.elements.btnCopyJson.click();
  await new Promise(r => setTimeout(r, 10));
  assert.ok(env.elements.copyStatus.classList.contains("is-error"));
  assert.ok(env.elements.copyStatus.textContent.includes("复制失败"));
  assert.ok(!env.elements.copyStatus.textContent.includes("已成功"));

  // 2. 剪贴板完全不存在
  const origClipboard = globalThis.navigator.clipboard;
  globalThis.navigator.clipboard = null;
  env.elements.btnCopyJson.click();
  assert.ok(env.elements.copyStatus.classList.contains("is-error"));
  assert.ok(env.elements.copyStatus.textContent.includes("不支持自动复制"));
  globalThis.navigator.clipboard = origClipboard;

  // 3. 弹窗拦截（window.open 返回 null 或 closed）
  env.setClipboardFail(false);
  env.elements.btnViewFull.click();
  globalThis.window.open = () => null; // 模拟被拦截
  env.elements.btnGotoGithub.click();
  await new Promise(r => setTimeout(r, 10));
  assert.ok(env.elements.copyStatus.innerHTML.includes("GitHub 在线编辑页"));
  assert.ok(env.elements.copyStatus.innerHTML.includes("拦截了自动弹出窗口"));
});

test("modals: 8.2.5 owned 原协议及前置条件不变，下载仍为 owned-patch.json，合并命令正确", async () => {
  const env = setupMockEnv();
  const overridesState = createOverridesState();
  const ownedState = createOwnedState();
  markOwned(ownedState, { skill_id: "owned/test-1", name: "测试已收录" });

  const modal = initSyncModal(env.elements, overridesState, null, ownedState);
  modal.openSyncModal();
  env.elements.tabModalOwned.click();

  assert.equal(modal.getCurrentTab(), "owned");
  assert.equal(env.elements.btnDownloadJson.textContent, "💾 下载 owned-patch.json");
  assert.equal(env.elements.btnGotoGithub.textContent, "📋 复制本地合并命令");

  // 下载检查
  env.elements.btnDownloadJson.click();
  assert.equal(env.downloads[0].download, "owned-patch.json");

  // 复制合并命令
  env.elements.btnGotoGithub.click();
  await new Promise(r => setTimeout(r, 10));
  assert.ok(env.getClipboard().includes(".\\.venv\\Scripts\\python.exe tools/manage_owned.py --apply-changes owned-patch.json"));
  assert.ok(env.elements.copyStatus.textContent.includes("已复制本地合并命令"));
});

test("modals: 8.2.6 所有预览/导出动作不清空暂存；明确放弃后的浮条与弹窗刷新一致", () => {
  const env = setupMockEnv();
  const overridesState = createOverridesState();
  togglePick(overridesState, "tool/persist", "recommended", "2026-09-29");

  let clearCalled = false;
  const modal = initSyncModal(env.elements, overridesState, () => { clearCalled = true; }, null);
  modal.openSyncModal();

  // 预览、复制、下载都不清空暂存
  env.elements.btnCopyJson.click();
  env.elements.btnDownloadJson.click();
  assert.equal(Object.keys(overridesState.stagedPicks).length, 1);

  // 点击清空
  env.elements.btnClearSync.click();
  assert.equal(clearCalled, true);
  assert.equal(Object.keys(overridesState.stagedPicks).length, 0);

  updateSyncBar(env.elements.syncBar, env.elements.syncSummary, overridesState, null);
  assert.equal(env.elements.syncBar.hidden, true);
});

test("modals: 关闭或重新打开后，延迟复制成功/失败均不能更新提示或打开网页", async () => {
  for (const reopen of [false, true]) {
    for (const reject of [false, true]) {
      for (const action of ["btnCopyJson", "btnGotoGithub"]) {
        const env = setupMockEnv();
        const state = createOverridesState();
        togglePick(state, "test/pending", "recommended", "2026-09-29");
        const modal = initSyncModal(env.elements, state, null);
        modal.openSyncModal();
        env.elements.btnViewFull.click();
        let resolveCopy, rejectCopy;
        navigator.clipboard.writeText = () => new Promise((resolve, fail) => {
          resolveCopy = resolve;
          rejectCopy = fail;
        });
        env.elements[action].click();
        modal.closeSyncModal();
        if (reopen) modal.openSyncModal();
        if (reject) rejectCopy(new Error("permission denied"));
        else resolveCopy();
        await Promise.resolve();
        await Promise.resolve();
        assert.equal(env.openedWindows.length, 0);
        assert.equal(env.elements.copyStatus.textContent, "");
        assert.equal(env.elements.syncModal.hidden, !reopen);
        if (reopen) assert.equal(modal.getCurrentViewMode(), "incremental");
      }
    }
  }
});

test("modals: 状态刷新后旧复制回调不能覆盖冲突警告或打开网页", async () => {
  const env = setupMockEnv();
  const state = createOverridesState();
  togglePick(state, "test/conflict", "recommended", "2026-09-29");
  const modal = initSyncModal(env.elements, state, null);
  modal.openSyncModal();
  env.elements.btnViewFull.click();
  let finish;
  navigator.clipboard.writeText = () => new Promise(resolve => { finish = resolve; });
  env.elements.btnGotoGithub.click();
  state.stagedExclusions["test/conflict"] = { skill_id: "test/conflict" };
  modal.updateModalContent();
  const warning = env.elements.copyStatus.textContent;
  finish();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(env.openedWindows.length, 0);
  assert.equal(env.elements.copyStatus.textContent, warning);
  assert.equal(env.elements.btnDownloadJson.disabled, true);
});

test("modals: 普通配置冲突不阻止独立 owned 变更包，返回配置页仍阻止导出", async () => {
  const env = setupMockEnv();
  const state = createOverridesState();
  state.baselinePicks["test/conflict"] = { skill_id: "test/conflict" };
  state.stagedExclusions["test/conflict"] = { skill_id: "test/conflict" };
  const owned = createOwnedState();
  markOwned(owned, { skill_id: "owned/independent", name: "独立条目" });
  const expectedPatch = generateOwnedPatch(owned);
  const modal = initSyncModal(env.elements, state, null, owned);
  modal.openSyncModal();
  assert.equal(env.elements.btnDownloadJson.disabled, true);
  env.elements.tabModalOwned.click();
  assert.equal(env.elements.jsonPreview.textContent, expectedPatch);
  assert.equal(env.elements.jsonViewToggles.hidden, true);
  assert.equal(env.elements.modalCrossWarning.hidden, true);
  assert.equal(env.elements.copyStatus.textContent, "");
  for (const name of ["btnDownloadJson", "btnCopyJson", "btnGotoGithub"]) {
    assert.equal(env.elements[name].disabled, false);
  }
  env.elements.btnDownloadJson.click();
  assert.equal(env.downloads[0].download, "owned-patch.json");
  env.elements.btnCopyJson.click();
  await Promise.resolve();
  assert.equal(env.getClipboard(), expectedPatch);
  env.elements.btnGotoGithub.click();
  await Promise.resolve();
  assert.ok(env.getClipboard().includes("tools/manage_owned.py --apply-changes owned-patch.json"));
  assert.equal(env.openedWindows.length, 0);
  for (const tab of ["tabModalOverrides", "tabModalSnoozed"]) {
    env.elements[tab].click();
    assert.equal(env.elements.btnDownloadJson.disabled, true);
    assert.equal(env.elements.btnCopyJson.disabled, true);
    assert.equal(env.elements.btnGotoGithub.disabled, true);
    assert.ok(env.elements.copyStatus.textContent.includes("冲突"));
  }
});

test("modals: 8.2.7 含 <script>、HTML 字符、引号及中文的条目只作为文本显示", () => {
  const env = setupMockEnv();
  const overridesState = createOverridesState();
  const sid = "<script>alert(1)</script>&\"test\"中文";
  togglePick(overridesState, sid, "recommended", "2026-09-29");

  const modal = initSyncModal(env.elements, overridesState, null, null);
  modal.openSyncModal();

  // 检查 jsonPreview 包含该字符串且为文本内容
  assert.ok(env.elements.jsonPreview.textContent.includes("<script>alert(1)</script>&\\\"test\\\"中文") ||
            env.elements.jsonPreview.textContent.includes(sid));
});
