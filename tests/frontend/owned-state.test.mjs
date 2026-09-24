/**
 * 前端已收录状态机单元测试 (Native Node.js test runner: node --test)。
 * 验证 owned-state.js 的纯函数状态转换、变更包生成、私人详情隔离与备份导入导出。
 */

import test from "node:test";
import assert from "node:assert/strict";

import {
  createOwnedState,
  populateOwnedBaseline,
  isOwned,
  markOwned,
  unmarkOwned,
  calculateOwnedChangesCount,
  generateOwnedPatch,
  getEffectiveOwnedList,
  validateManagedUrl,
  setPrivateDetails,
  getPrivateDetails,
  removePrivateDetails,
  exportPrivateBackup,
  importPrivateBackup,
  saveOwnedStagedStorage,
  loadOwnedStagedStorage,
  clearOwnedStagedStorage,
  saveOwnedPrivateStorage,
  loadOwnedPrivateStorage,
  reconcileOwnedStaged,
  STORAGE_KEY_OWNED_STAGED,
  STORAGE_KEY_OWNED_PRIVATE
} from "../../public/js/owned-state.js";

test("owned-state: baseline population and isOwned query", () => {
  const state = createOwnedState();
  assert.equal(isOwned(state, "owner/repo:skills/pdf/SKILL.md"), false);

  populateOwnedBaseline(state, {
    items: [
      {
        skill_id: "owner/repo:skills/pdf/SKILL.md",
        name: "pdf",
        source_url: "https://github.com/owner/repo",
        added_at: "2026-09-24"
      }
    ]
  }, [
    {
      skill_id: "owner/repo:skills/pdf/SKILL.md",
      name: "pdf",
      original_partition: "recommended"
    }
  ]);

  assert.equal(isOwned(state, "owner/repo:skills/pdf/SKILL.md"), true);
  assert.equal(isOwned(state, "other/repo:SKILL.md"), false);
});

test("owned-state: markOwned and immediate unmark cancel out without fake delete", () => {
  const state = createOwnedState();
  const sid = "owner/new-repo:skills/tool/SKILL.md";

  // 1. 标记已收录
  markOwned(state, { skill_id: sid, name: "tool", url: "https://github.com/owner/new-repo" }, "2026-09-24");
  assert.equal(isOwned(state, sid), true);
  assert.equal(calculateOwnedChangesCount(state), 1);

  // 2. 立即取消：应抵消该次新增，待同步变更归零，不产生虚假删除
  unmarkOwned(state, sid);
  assert.equal(isOwned(state, sid), false);
  assert.equal(calculateOwnedChangesCount(state), 0);
  assert.equal(state.stagedDeletes.size, 0);

  const patch = JSON.parse(generateOwnedPatch(state));
  assert.equal(patch.changes.length, 0);
});

test("owned-state: unmarking baseline item produces before-after patch and cancels on re-mark", () => {
  const state = createOwnedState();
  const sid = "owner/repo:skills/pdf/SKILL.md";
  populateOwnedBaseline(state, {
    items: [
      { skill_id: sid, name: "pdf", source_url: "https://example.com", added_at: "2026-09-24" }
    ]
  });

  // 1. 取消已收录基线项
  unmarkOwned(state, sid);
  assert.equal(isOwned(state, sid), false);
  assert.equal(calculateOwnedChangesCount(state), 1);

  const patchJson = generateOwnedPatch(state);
  const patch = JSON.parse(patchJson);
  assert.equal(patch.changes.length, 1);
  assert.equal(patch.changes[0].skill_id, sid);
  assert.equal(patch.changes[0].before.name, "pdf");
  assert.equal(patch.changes[0].after, null);

  // 2. 重新标记：撤销待删除标记，恢复基线
  markOwned(state, { skill_id: sid });
  assert.equal(isOwned(state, sid), true);
  assert.equal(calculateOwnedChangesCount(state), 0);
  assert.equal(state.stagedDeletes.size, 0);
});

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

test("owned-state: validateManagedUrl security rules", () => {
  assert.equal(validateManagedUrl(""), "");
  assert.equal(validateManagedUrl(null), "");
  assert.equal(validateManagedUrl("https://github.com/my/fork"), "https://github.com/my/fork");
  assert.equal(validateManagedUrl("http://localhost:3000/skills"), "http://localhost:3000/skills");

  assert.throws(() => validateManagedUrl("javascript:alert(1)"), /仅支持 http:\/\/ 或 https:\/\//);
  assert.throws(() => validateManagedUrl("data:text/html,bad"), /仅支持 http:\/\/ 或 https:\/\//);
  assert.throws(() => validateManagedUrl("https://user:pass@github.com/repo"), /不能包含用户名或密码凭据/);
  assert.throws(() => validateManagedUrl("not a url"), /无效的管理链接 URL/);
});

test("owned-state: private backup export and import with conflict strategies", () => {
  const state = createOwnedState();
  const sid1 = "owner/repo1:SKILL.md";
  const sid2 = "owner/repo2:SKILL.md";

  setPrivateDetails(state, sid1, {
    managed_url: "https://myfork1.org",
    note: "本地备注1"
  });

  const exported = exportPrivateBackup(state);
  const parsed = JSON.parse(exported);
  assert.equal(parsed.schema_version, "1.0.0");
  assert.equal(parsed.items[sid1].note, "本地备注1");

  // 测试导入：默认保留本地策略 (keep_local)
  const incoming = {
    schema_version: "1.0.0",
    items: {
      [sid1]: { managed_url: "https://remote.org", note: "远端备注1" },
      [sid2]: { managed_url: "https://myfork2.org", note: "新条目2" }
    }
  };

  const resKeep = importPrivateBackup(state, incoming, { conflictStrategy: "keep_local" });
  assert.equal(resKeep.conflictCount, 1);
  assert.equal(getPrivateDetails(state, sid1).note, "本地备注1"); // 保留本地
  assert.equal(getPrivateDetails(state, sid2).note, "新条目2");  // 新增无冲突

  // 测试导入：覆盖使用导入策略 (use_imported)
  const resUse = importPrivateBackup(state, incoming, { conflictStrategy: "use_imported" });
  assert.equal(resUse.conflictCount, 1);
  assert.equal(getPrivateDetails(state, sid1).note, "远端备注1"); // 覆盖
});

test("owned-state: LocalStorage persistence of staged and private data", () => {
  const stagedStore = {};
  const mockStorage = {
    getItem: key => stagedStore[key] || null,
    setItem: (key, val) => { stagedStore[key] = String(val); },
    removeItem: key => { delete stagedStore[key]; }
  };

  const state1 = createOwnedState();
  markOwned(state1, { skill_id: "test/item:SKILL.md", name: "test" }, "2026-09-24");
  setPrivateDetails(state1, "test/item:SKILL.md", { managed_url: "https://fork.com", note: "我的笔记" });

  saveOwnedStagedStorage(state1, mockStorage);
  saveOwnedPrivateStorage(state1, mockStorage);

  const state2 = createOwnedState();
  loadOwnedStagedStorage(state2, mockStorage);
  loadOwnedPrivateStorage(state2, mockStorage);

  assert.equal(isOwned(state2, "test/item:SKILL.md"), true);
  assert.equal(getPrivateDetails(state2, "test/item:SKILL.md").note, "我的笔记");

  clearOwnedStagedStorage(state2, mockStorage);
  assert.equal(isOwned(state2, "test/item:SKILL.md"), false);
  // 清除暂存不删除私人详情
  assert.equal(getPrivateDetails(state2, "test/item:SKILL.md").note, "我的笔记");
});

test("owned-state: O-01 reconcileOwnedStaged clears synced adds and deletes", () => {
  const state = createOwnedState();
  const sidSynced = "owner/repo1:SKILL.md";
  const sidUnsynced = "owner/repo2:SKILL.md";
  const sidDeletedSynced = "owner/repo3:SKILL.md";
  const sidDeletedPending = "owner/repo4:SKILL.md";

  // 基线包含 sidSynced 和 sidDeletedPending
  populateOwnedBaseline(state, {
    items: [
      { skill_id: sidSynced, name: "item1" },
      { skill_id: sidDeletedPending, name: "item4" }
    ]
  });

  // 浏览器中有旧的暂存新增和删除
  state.stagedAdds[sidSynced] = { skill_id: sidSynced, name: "item1" }; // 已合入基线
  state.stagedAdds[sidUnsynced] = { skill_id: sidUnsynced, name: "item2" }; // 未合入基线
  state.stagedDeletes.add(sidDeletedSynced); // 基线中已没有 item3，说明删除已合入
  state.stagedDeletes.add(sidDeletedPending); // 基线中还有 item4，说明删除尚未合入

  const res = reconcileOwnedStaged(state);
  assert.equal(res.reconciledAdds, 1);
  assert.equal(res.reconciledDeletes, 1);

  // 验证状态
  assert.equal(Boolean(state.stagedAdds[sidSynced]), false, "已合入基线的暂存新增应被对账清除");
  assert.equal(Boolean(state.stagedAdds[sidUnsynced]), true, "未合入基线的新增应保留");
  assert.equal(state.stagedDeletes.has(sidDeletedSynced), false, "基线已无该项的暂存删除应被对账清除");
  assert.equal(state.stagedDeletes.has(sidDeletedPending), true, "基线尚存该项的删除应继续保留");
});

test("owned-state: O-01 unmarking baseline item with stale stagedAdd still records stagedDeletes", () => {
  const state = createOwnedState();
  const sid = "owner/repo:SKILL.md";

  // 模拟对账前的双重状态：基线有此项，本地 LocalStorage 也有旧 stagedAdds
  populateOwnedBaseline(state, {
    items: [{ skill_id: sid, name: "my-skill", source_url: "https://github.com/owner/repo", added_at: "2026-09-24" }]
  });
  state.stagedAdds[sid] = { skill_id: sid, name: "my-skill" };

  // 取消已收录
  unmarkOwned(state, sid);

  // 验收断言：
  // 1. isOwned 不再判为已收录
  assert.equal(isOwned(state, sid), false);
  // 2. stagedAdds 被清除
  assert.equal(Boolean(state.stagedAdds[sid]), false);
  // 3. stagedDeletes 必须包含 sid
  assert.equal(state.stagedDeletes.has(sid), true);
  // 4. 生成的补丁必须包含有效删除记录（before 为基线项，after 为 null）
  const patch = JSON.parse(generateOwnedPatch(state));
  assert.equal(patch.changes.length, 1);
  assert.equal(patch.changes[0].skill_id, sid);
  assert.equal(patch.changes[0].before.name, "my-skill");
  assert.equal(patch.changes[0].after, null);
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

test("owned-state: 操作 → 保存 → 刷新全过程：取消已收录不被缓存复活，待同步数量清零", () => {
  const storage = {};
  const mockStorage = {
    getItem: key => storage[key] || null,
    setItem: (key, val) => { storage[key] = String(val); },
    removeItem: key => { delete storage[key]; }
  };

  const sid = "owner/skill-a:SKILL.md";

  // 1. 操作：标记 Skill A 为已收录
  const state1 = createOwnedState();
  markOwned(state1, { skill_id: sid, name: "Skill A" });
  assert.equal(isOwned(state1, sid), true);
  assert.equal(calculateOwnedChangesCount(state1), 1);

  // 2. 保存至本地缓存
  saveOwnedStagedStorage(state1, mockStorage);
  assert.ok(storage[STORAGE_KEY_OWNED_STAGED]);

  // 3. 操作：点击撤销 / 取消已收录
  unmarkOwned(state1, sid);
  assert.equal(isOwned(state1, sid), false);
  assert.equal(calculateOwnedChangesCount(state1), 0);

  // 4. 保存至本地缓存
  saveOwnedStagedStorage(state1, mockStorage);

  // 5. 模拟刷新浏览器：全新状态机从缓存载入
  const state2 = createOwnedState();
  loadOwnedStagedStorage(state2, mockStorage);

  // 验收：绝不从缓存复活，Skill 不再处于已收录状态，待同步数量为 0
  assert.equal(isOwned(state2, sid), false, "刷新后条目不应被缓存复活");
  assert.equal(state2.stagedAdds[sid], undefined, "暂存新增中不应残留该条目");
  assert.equal(state2.stagedDeletes.has(sid), false, "暂存删除中不应产生虚假删除");
  assert.equal(calculateOwnedChangesCount(state2), 0, "待同步数量必须清零");
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

test("owned-state: 操作 → 保存 → 刷新全过程：私人备注删除或清空后绝不从缓存复活", () => {
  const storage = {};
  const mockStorage = {
    getItem: key => storage[key] || null,
    setItem: (key, val) => { storage[key] = String(val); },
    removeItem: key => { delete storage[key]; }
  };

  const sid = "owner/private-item:SKILL.md";

  // 1. 设置私人详情并保存
  const state1 = createOwnedState();
  setPrivateDetails(state1, sid, { managed_url: "https://my-fork.internal", note: "我的重要备注" });
  saveOwnedPrivateStorage(state1, mockStorage);

  assert.equal(getPrivateDetails(state1, sid).note, "我的重要备注");

  // 2. 用户点击“删除私人详情”
  removePrivateDetails(state1, sid);
  assert.equal(getPrivateDetails(state1, sid).note, "");

  // 3. 保存
  saveOwnedPrivateStorage(state1, mockStorage);

  // 4. 模拟刷新
  const state2 = createOwnedState();
  loadOwnedPrivateStorage(state2, mockStorage);

  // 验收：刷新后私人详情彻底删除，不从缓存复活
  assert.equal(getPrivateDetails(state2, sid).note, "");
  assert.equal(getPrivateDetails(state2, sid).managed_url, "");
  assert.equal(state2.privateDetails[sid], undefined);

  // 5. 同样验证清空字段后保存的删除效果
  setPrivateDetails(state2, sid, { managed_url: "https://test.org", note: "临时备注" });
  saveOwnedPrivateStorage(state2, mockStorage);
  assert.equal(getPrivateDetails(state2, sid).note, "临时备注");

  setPrivateDetails(state2, sid, { managed_url: "", note: "" }); // 清空保存
  saveOwnedPrivateStorage(state2, mockStorage);

  const state3 = createOwnedState();
  loadOwnedPrivateStorage(state3, mockStorage);
  assert.equal(getPrivateDetails(state3, sid).note, "");
  assert.equal(state3.privateDetails[sid], undefined);
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

  // 模拟任一标签页刷新
  const refreshed = createOwnedState();
  loadOwnedPrivateStorage(refreshed, mockStorage);

  // 验收：
  // 1. Skill A 的新备注没有被标签页乙的旧快照覆盖！
  assert.equal(getPrivateDetails(refreshed, sidA).note, "Note A v2 (new by Tab 1)");
  // 2. Skill B 的新备注正常保存！
  assert.equal(getPrivateDetails(refreshed, sidB).note, "Note B v2 (new by Tab 2)");

  // 3. 版本检查：若标签页甲试图以陈旧时间戳 (t=1005 < 1020) 保存 Skill B，将被判定为旧快照而拒绝覆盖
  setPrivateDetails(tab1, sidB, { note: "Stale B note" }, 1005);
  saveOwnedPrivateStorage(tab1, mockStorage);

  const finalCheck = createOwnedState();
  loadOwnedPrivateStorage(finalCheck, mockStorage);
  assert.equal(getPrivateDetails(finalCheck, sidB).note, "Note B v2 (new by Tab 2)", "旧版本的覆盖应被版本检查拦截");
});
