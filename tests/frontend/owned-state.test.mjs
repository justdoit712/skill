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
  reconcileOwnedStaged
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
