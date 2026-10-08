/**
 * 前端状态机单元测试 (Native Node.js test runner: node --test)。
 * 验证状态变更与本地存储持久化。
 */

import test from "node:test";
import assert from "node:assert/strict";

import {
  createOverridesState,
  isPicked,
  togglePick,
  saveStorage,
  loadStorage,
  clearStorage
} from "../../public/js/catalog-state.js";

test("catalog-state: mock storage save, load, and clear", () => {
  const store = {};
  const mockStorage = {
    getItem: key => store[key] || null,
    setItem: (key, val) => { store[key] = String(val); },
    removeItem: key => { delete store[key]; }
  };

  const state1 = createOverridesState();
  togglePick(state1, "test/p", "recommended", "2026-09-23");
  saveStorage(state1, "test_storage", mockStorage);
  assert.ok(store["test_storage"]);

  const state2 = createOverridesState();
  loadStorage(state2, "test_storage", "legacy", mockStorage);
  assert.equal(isPicked(state2, "test/p"), true);

  clearStorage(state2, "test_storage", "legacy", mockStorage);
  assert.equal(store["test_storage"], undefined);
  assert.equal(isPicked(state2, "test/p"), false);
});
