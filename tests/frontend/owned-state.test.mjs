import test from "node:test";
import assert from "node:assert/strict";

import {
  createOwnedState,
  isOwned,
  markOwned,
  setPrivateDetails,
  getPrivateDetails,
  removePrivateDetails,
  populateOwnedBaseline,
  reconcileOwnedStaged,
  calculateOwnedChangesCount,
  generateOwnedPatch,
  saveOwnedStagedStorage,
  loadOwnedStagedStorage,
  saveOwnedPrivateStorage,
  loadOwnedPrivateStorage
} from "../../public/js/owned-state.js";

test("owned-state: patch never contains private details", () => {
  const state = createOwnedState();
  const sid = "owner/repo:skills/secret/SKILL.md";
  markOwned(state, { skill_id: sid, name: "secret" }, "2026-09-24");
  setPrivateDetails(state, sid, {
    managed_url: "https://private.internal/repo",
    note: "绝密备注信息"
  });

  // 私人详情不计入待同步变更数
  assert.equal(calculateOwnedChangesCount(state), 1);

  const patchJson = generateOwnedPatch(state);
  assert.doesNotMatch(patchJson, /private\.internal/);
  assert.doesNotMatch(patchJson, /绝密备注信息/);
  assert.doesNotMatch(patchJson, /managed_url/);
  assert.doesNotMatch(patchJson, /note/);
});

test("owned-state: 删除或清空私人详情后保存刷新不复活，其他备注保留", () => {
  const data = new Map();
  const storage = {
    getItem: key => data.get(key) ?? null,
    setItem: (key, value) => data.set(key, String(value))
  };
  const deleted = "owner/deleted:SKILL.md";
  const cleared = "owner/cleared:SKILL.md";
  const kept = "owner/kept:SKILL.md";
  const initial = createOwnedState();
  for (const sid of [deleted, cleared, kept]) {
    setPrivateDetails(initial, sid, {
      managed_url: "https://private.example/skill",
      note: "已保存的私人备注"
    }, 1000);
  }
  saveOwnedPrivateStorage(initial, storage);

  const editing = createOwnedState();
  loadOwnedPrivateStorage(editing, storage);
  assert.equal(getPrivateDetails(editing, deleted).note, "已保存的私人备注");
  assert.equal(getPrivateDetails(editing, cleared).note, "已保存的私人备注");
  removePrivateDetails(editing, deleted, 2000);
  setPrivateDetails(editing, cleared, { managed_url: "", note: "   " }, 2000);
  saveOwnedPrivateStorage(editing, storage);

  // 从持久化数据创建全新状态，不能只检查删除后的内存对象。
  const refreshed = createOwnedState();
  loadOwnedPrivateStorage(refreshed, storage);
  for (const sid of [deleted, cleared]) {
    assert.equal(Object.hasOwn(refreshed.privateDetails, sid), false);
    assert.deepEqual(getPrivateDetails(refreshed, sid), { managed_url: "", note: "" });
  }
  assert.equal(getPrivateDetails(refreshed, kept).note, "已保存的私人备注");
  assert.equal(getPrivateDetails(refreshed, kept).managed_url, "https://private.example/skill");
});

test("owned-state: O-02 multi-tab incremental merge prevents state loss in staged and private storage", () => {
  const storage = {};
  const mockStorage = {
    getItem: key => storage[key] || null,
    setItem: (key, val) => { storage[key] = String(val); },
    removeItem: key => { delete storage[key]; }
  };

  // 标签页甲：标记 itemA，保存到存储
  const tabA = createOwnedState();
  markOwned(tabA, { skill_id: "owner/tabA:SKILL.md", name: "tabA" }, "2026-09-24");
  setPrivateDetails(tabA, "owner/tabA:SKILL.md", { note: "Tab A note" });
  saveOwnedStagedStorage(tabA, mockStorage);
  saveOwnedPrivateStorage(tabA, mockStorage);

  // 标签页乙：标记 itemB，内存中初始没有 itemA
  const tabB = createOwnedState();
  markOwned(tabB, { skill_id: "owner/tabB:SKILL.md", name: "tabB" }, "2026-09-24");
  setPrivateDetails(tabB, "owner/tabB:SKILL.md", { note: "Tab B note" });
  // 保存时自动增量合并存储中已存在的 itemA
  saveOwnedStagedStorage(tabB, mockStorage);
  saveOwnedPrivateStorage(tabB, mockStorage);

  // 此时存储中应该同时包含 itemA 和 itemB，甲的操作没有被乙覆盖
  const stagedData = JSON.parse(storage["skills_catalog_owned_staged_v1"]);
  assert.ok(stagedData.stagedAdds["owner/tabA:SKILL.md"], "Tab A 的新增未被 Tab B 覆盖");
  assert.ok(stagedData.stagedAdds["owner/tabB:SKILL.md"], "Tab B 的新增正常保存");

  const privateData = JSON.parse(storage["skills_catalog_owned_private_v1"]);
  assert.equal(privateData["owner/tabA:SKILL.md"].note, "Tab A note");
  assert.equal(privateData["owner/tabB:SKILL.md"].note, "Tab B note");
});

test("owned-state: 操作 → 保存 → 刷新全过程：同步合入基线后对账与保存，待同步数量清零且刷新不再反复出现", () => {
  const storage = {};
  const mockStorage = {
    getItem: key => storage[key] || null,
    setItem: (key, val) => { storage[key] = String(val); },
    removeItem: key => { delete storage[key]; }
  };

  const sid = "owner/synced-skill:SKILL.md";

  // 1. 此前页面标记并保存了暂存
  const state1 = createOwnedState();
  markOwned(state1, { skill_id: sid, name: "Synced Skill" });
  saveOwnedStagedStorage(state1, mockStorage);

  // 2. 仓库同步完成，重新打开/刷新页面：基线中已包含该 Skill
  const bootState = createOwnedState();
  populateOwnedBaseline(bootState, {
    items: [{ skill_id: sid, name: "Synced Skill" }]
  });
  loadOwnedStagedStorage(bootState, mockStorage);
  assert.equal(calculateOwnedChangesCount(bootState), 1, "对账前处于暂存状态");

  // 3. 执行对账与保存
  const reconcileRes = reconcileOwnedStaged(bootState);
  assert.equal(reconcileRes.reconciledAdds, 1);
  saveOwnedStagedStorage(bootState, mockStorage);

  // 4. 再次刷新页面验证
  const refreshedState = createOwnedState();
  populateOwnedBaseline(refreshedState, {
    items: [{ skill_id: sid, name: "Synced Skill" }]
  });
  loadOwnedStagedStorage(refreshedState, mockStorage);

  // 验收：待同步数量彻底清零，暂存区不再残留已同步记录
  assert.equal(isOwned(refreshedState, sid), true, "基线存在，状态仍为已收录");
  assert.equal(refreshedState.stagedAdds[sid], undefined, "暂存区不再有该条目");
  assert.equal(calculateOwnedChangesCount(refreshedState), 0, "待同步数量清零");
});

test("owned-state: 多标签页并发修改备注：版本检查确保新备注不被旧页面快照覆盖", () => {
  const storage = {};
  const mockStorage = {
    getItem: key => storage[key] || null,
    setItem: (key, val) => { storage[key] = String(val); },
    removeItem: key => { delete storage[key]; }
  };

  const sidA = "owner/skill-a:SKILL.md";
  const sidB = "owner/skill-b:SKILL.md";

  // 初始状态 (t=1000)：两个 Skill 均有初始备注
  const initTab = createOwnedState();
  setPrivateDetails(initTab, sidA, { note: "Note A v1" }, 1000);
  setPrivateDetails(initTab, sidB, { note: "Note B v1" }, 1000);
  saveOwnedPrivateStorage(initTab, mockStorage);

  // 标签页甲与乙均在 t=1000 打开并载入快照
  const tab1 = createOwnedState();
  loadOwnedPrivateStorage(tab1, mockStorage);

  const tab2 = createOwnedState();
  loadOwnedPrivateStorage(tab2, mockStorage);

  // 标签页甲在 t=1010 修改 Skill A 为新备注并保存
  setPrivateDetails(tab1, sidA, { note: "Note A v2 (new by Tab 1)" }, 1010);
  saveOwnedPrivateStorage(tab1, mockStorage);

  // 标签页乙尚未收到更新（本地内存仍是 Note A v1），在 t=1020 修改 Skill B 并保存
  setPrivateDetails(tab2, sidB, { note: "Note B v2 (new by Tab 2)" }, 1020);
  saveOwnedPrivateStorage(tab2, mockStorage);

  // 模拟另一个标签页刷新
  const refreshed = createOwnedState();
  loadOwnedPrivateStorage(refreshed, mockStorage);

  // 验收：
  // 1. Skill A 的新备注没有被标签页乙的旧快照覆盖！
  assert.equal(getPrivateDetails(refreshed, sidA).note, "Note A v2 (new by Tab 1)");
  // 2. Skill B 的新备注正常保存！
  assert.equal(getPrivateDetails(refreshed, sidB).note, "Note B v2 (new by Tab 2)");

  // 3. 版本检查：标签页甲试图以更旧时间戳 (t=1005 < 1020) 写入 Skill B，判定为旧快照并拒绝覆盖
  setPrivateDetails(tab1, sidB, { note: "Stale B note" }, 1005);
  saveOwnedPrivateStorage(tab1, mockStorage);

  const finalCheck = createOwnedState();
  loadOwnedPrivateStorage(finalCheck, mockStorage);
  assert.equal(getPrivateDetails(finalCheck, sidB).note, "Note B v2 (new by Tab 2)", "旧版本的覆盖应被版本检查拒绝");
});
