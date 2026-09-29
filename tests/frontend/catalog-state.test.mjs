/**
 * 前端状态机单元测试 (Native Node.js test runner: node --test)。
 * 验证 catalog-state.js 的纯函数状态转换、互斥关系、分区计算与 JSON 导出。
 */

import test from "node:test";
import assert from "node:assert/strict";

import {
  createOverridesState,
  isPicked,
  isExcluded,
  isSnoozed,
  getEffectiveSnoozedList,
  togglePick,
  blockSkill,
  snoozeSkill,
  unsnoozeSkill,
  calculateChangesCount,
  generateOverridesJson,
  generateSnoozedJson,
  computeEffectivePicks,
  computeEffectiveExclusions,
  computeEffectiveSnoozed,
  detectConflicts,
  isDeepEqual,
  diffCollection,
  computeOverridesDiff,
  computeSnoozedDiff,
  generateIncrementalOverridesJson,
  generateIncrementalSnoozedJson,
  populateBaseline,
  partitionEntries,
  saveStorage,
  loadStorage,
  clearStorage
} from "../../public/js/catalog-state.js";

import { computeExpiresAt, isSnoozeActive } from "../../public/js/utils.js";

test("utils: computeExpiresAt & isSnoozeActive", () => {
  const expires = computeExpiresAt("2026-01-01", 150);
  assert.equal(expires, "2026-05-31");

  const item = { snoozed_at: "2026-01-01", expires_at: "2026-05-31" };
  assert.equal(isSnoozeActive(item, "2026-01-01"), true);
  assert.equal(isSnoozeActive(item, "2026-03-01"), true);
  assert.equal(isSnoozeActive(item, "2026-05-30"), true);
  assert.equal(isSnoozeActive(item, "2026-05-31"), false); // 到期当天自动恢复
  assert.equal(isSnoozeActive(item, "2026-06-01"), false);
});

test("catalog-state: togglePick adds and removes pick", () => {
  const state = createOverridesState();
  const sid = "author/tool:SKILL.md";

  assert.equal(isPicked(state, sid), false);
  togglePick(state, sid, "recommended", "2026-09-23");
  assert.equal(isPicked(state, sid), true);
  assert.equal(calculateChangesCount(state), 1);

  // 再次点击取消收藏
  togglePick(state, sid, "recommended", "2026-09-23");
  assert.equal(isPicked(state, sid), false);
  assert.equal(calculateChangesCount(state), 0);
});

test("catalog-state: blockSkill adds exclusion and clears pick and snooze", () => {
  const state = createOverridesState();
  const sid = "author/bad-tool:SKILL.md";

  // 先收藏
  togglePick(state, sid, "recommended", "2026-09-23");
  assert.equal(isPicked(state, sid), true);

  // 屏蔽条目
  blockSkill(state, sid, "2026-09-23");
  assert.equal(isPicked(state, sid), false, "屏蔽应自动清除收藏");
  assert.equal(isExcluded(state, sid), true, "条目应被标记为排除");
});

test("catalog-state: snoozeSkill adds snooze and clears pick and exclusion", () => {
  const state = createOverridesState();
  const sid = "author/sleepy-tool:SKILL.md";

  // 先屏蔽
  blockSkill(state, sid, "2026-09-23");
  assert.equal(isExcluded(state, sid), true);

  // 冷冻
  snoozeSkill(state, sid, "2026-09-23", 150);
  assert.equal(isExcluded(state, sid), false, "冷冻应自动清除排除状态");
  assert.equal(isSnoozed(state, sid, "2026-09-23"), true);

  // 取消冷冻
  unsnoozeSkill(state, sid);
  assert.equal(isSnoozed(state, sid, "2026-09-23"), false);
});

test("catalog-state: partitionEntries partitions entries correctly", () => {
  const state = createOverridesState();
  const allEntries = {
    "rec/tool1": { skill_id: "rec/tool1", _baselineTab: "recommended" },
    "rec/tool2": { skill_id: "rec/tool2", _baselineTab: "recommended" },
    "cand/tool3": { skill_id: "cand/tool3", _baselineTab: "candidate" },
    "cand/tool4": { skill_id: "cand/tool4", _baselineTab: "candidate" }
  };

  // 收藏 cand/tool3
  togglePick(state, "cand/tool3", "candidate", "2026-09-23");
  // 屏蔽 rec/tool2
  blockSkill(state, "rec/tool2", "2026-09-23");
  // 冷冻 cand/tool4
  snoozeSkill(state, "cand/tool4", "2026-09-23", 150);

  const { activeRecommended, activeCandidates, activeManual } = partitionEntries(
    allEntries,
    state,
    "2026-09-23"
  );

  assert.equal(activeRecommended.length, 1);
  assert.equal(activeRecommended[0].skill_id, "rec/tool1");

  assert.equal(activeCandidates.length, 0); // cand/tool3 进入 manual，cand/tool4 进入冷冻
  assert.equal(activeManual.length, 1);
  assert.equal(activeManual[0].skill_id, "cand/tool3");

  const effectiveSnoozed = getEffectiveSnoozedList(state, "2026-09-23");
  assert.equal(effectiveSnoozed.length, 1);
  assert.equal(effectiveSnoozed[0].skill_id, "cand/tool4");
});

test("catalog-state: generateOverridesJson and generateSnoozedJson", () => {
  const state = createOverridesState();
  togglePick(state, "my/pick", "recommended", "2026-09-23");
  blockSkill(state, "my/block", "2026-09-23");
  snoozeSkill(state, "my/snooze", "2026-09-23", 150);

  const overridesJson = generateOverridesJson(state);
  const overridesObj = JSON.parse(overridesJson);
  assert.equal(overridesObj.overrides_version, "1.0.0");
  assert.equal(overridesObj.manual_picks.length, 1);
  assert.equal(overridesObj.manual_picks[0].skill_id, "my/pick");
  assert.equal(overridesObj.manual_exclusions.length, 1);
  assert.equal(overridesObj.manual_exclusions[0].skill_id, "my/block");

  const snoozedJson = generateSnoozedJson(state);
  const snoozedObj = JSON.parse(snoozedJson);
  assert.equal(snoozedObj.snooze_version, "1.0.0");
  assert.equal(snoozedObj.snoozed.length, 1);
  assert.equal(snoozedObj.snoozed[0].skill_id, "my/snooze");
});

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

test("catalog-state: O-03 partitionEntries strictly excludes non-display statuses (excluded, pending, processing_failure)", () => {
  const overrides = createOverridesState();
  const allEntries = {
    "rec/item": { skill_id: "rec/item", status: "recommended", _baselineTab: "recommended" },
    "cand/item": { skill_id: "cand/item", status: "candidate", _baselineTab: "candidate" },
    "excl/item": { skill_id: "excl/item", status: "excluded", _baselineTab: "excluded" },
    "pending/item": { skill_id: "pending/item", status: "pending", _baselineTab: "pending" },
    "failed/item": { skill_id: "failed/item", status: "processing_failure", _baselineTab: "processing_failure" },
    "unknown/item": { skill_id: "unknown/item", status: "custom_status", _baselineTab: "unknown" }
  };

  const { activeRecommended, activeCandidates } = partitionEntries(
    allEntries,
    overrides,
    "2026-09-24",
    null
  );

  // 严格白名单验证：
  // 1. recommended 仅包含 rec/item
  assert.equal(activeRecommended.length, 1);
  assert.equal(activeRecommended[0].skill_id, "rec/item");

  // 2. candidate 仅包含 cand/item
  assert.equal(activeCandidates.length, 1);
  assert.equal(activeCandidates[0].skill_id, "cand/item");

  // 3. excluded, pending, processing_failure, unknown 决不漏入候选区或推荐区
  const allActiveIds = [...activeRecommended, ...activeCandidates].map(e => e.skill_id);
  assert.ok(!allActiveIds.includes("excl/item"), "excluded 项决不展示在候选或推荐区");
  assert.ok(!allActiveIds.includes("pending/item"), "pending 项决不展示在候选或推荐区");
  assert.ok(!allActiveIds.includes("failed/item"), "processing_failure 项决不展示在候选或推荐区");
  assert.ok(!allActiveIds.includes("unknown/item"), "未知状态项决不展示在候选或推荐区");
});

test("catalog-state: 8.1.1 大量基线中只新增一项收藏：净差异只含该项，完整快照保留原集合", () => {
  const state = createOverridesState();
  for (let i = 1; i <= 50; i++) {
    state.baselinePicks[`base/skill-${i}`] = {
      skill_id: `base/skill-${i}`,
      reason: "原有基线",
      added_at: "2026-09-01",
      from: "recommended"
    };
  }
  togglePick(state, "new/awesome-tool", "recommended", "2026-09-29");

  const diff = computeOverridesDiff(state);
  assert.equal(diff.summary.changed_records, 1);
  assert.equal(diff.summary.affected_skills, 1);
  assert.equal(diff.changes.manual_picks.added.length, 1);
  assert.equal(diff.changes.manual_picks.added[0].skill_id, "new/awesome-tool");
  assert.equal(diff.changes.manual_picks.updated.length, 0);
  assert.equal(diff.changes.manual_picks.removed.length, 0);

  const fullJson = generateOverridesJson(state);
  const fullObj = JSON.parse(fullJson);
  assert.equal(fullObj.manual_picks.length, 51);
  assert.ok(fullObj.manual_picks.some(p => p.skill_id === "new/awesome-tool"));
  assert.ok(fullObj.manual_picks.some(p => p.skill_id === "base/skill-1"));
});

test("catalog-state: 8.1.2 删除基线收藏、取消屏蔽、解除冷冻：显示明确删除记录及原值", () => {
  const state = createOverridesState();
  state.baselinePicks["pick/1"] = { skill_id: "pick/1", reason: "基线收藏", added_at: "2026-09-01", from: "recommended" };
  state.baselineExclusions["excl/1"] = { skill_id: "excl/1", reason: "基线排除", added_at: "2026-09-01" };
  state.baselineSnoozed["snooze/1"] = { skill_id: "snooze/1", reason: "基线冷冻", snoozed_at: "2026-09-01", expires_at: "2027-01-29", days: 150 };

  togglePick(state, "pick/1");
  state.removedExclusions.add("excl/1");
  unsnoozeSkill(state, "snooze/1");

  const oDiff = computeOverridesDiff(state);
  assert.equal(oDiff.changes.manual_picks.removed.length, 1);
  assert.equal(oDiff.changes.manual_picks.removed[0].skill_id, "pick/1");
  assert.deepEqual(oDiff.changes.manual_picks.removed[0].before, state.baselinePicks["pick/1"]);

  assert.equal(oDiff.changes.manual_exclusions.removed.length, 1);
  assert.equal(oDiff.changes.manual_exclusions.removed[0].skill_id, "excl/1");
  assert.deepEqual(oDiff.changes.manual_exclusions.removed[0].before, state.baselineExclusions["excl/1"]);

  const sDiff = computeSnoozedDiff(state);
  assert.equal(sDiff.changes.snoozed.removed.length, 1);
  assert.equal(sDiff.changes.snoozed.removed[0].skill_id, "snooze/1");
  assert.deepEqual(sDiff.changes.snoozed.removed[0].before, state.baselineSnoozed["snooze/1"]);
});

test("catalog-state: 8.1.3 修改同 ID 的 reason/日期/期限：显示更新，不误标新增", () => {
  const state = createOverridesState();
  state.baselinePicks["pick/update"] = { skill_id: "pick/update", reason: "旧原因", added_at: "2026-09-01", from: "recommended" };
  state.stagedPicks["pick/update"] = { skill_id: "pick/update", reason: "新原因", added_at: "2026-09-29", from: "manual" };

  state.baselineSnoozed["snooze/update"] = { skill_id: "snooze/update", snoozed_at: "2026-09-01", expires_at: "2027-01-29", days: 150, reason: "原原因" };
  state.stagedSnoozed["snooze/update"] = { skill_id: "snooze/update", snoozed_at: "2026-09-29", expires_at: "2027-02-26", days: 150, reason: "延长冷冻" };

  const oDiff = computeOverridesDiff(state);
  assert.equal(oDiff.changes.manual_picks.added.length, 0);
  assert.equal(oDiff.changes.manual_picks.removed.length, 0);
  assert.equal(oDiff.changes.manual_picks.updated.length, 1);
  assert.equal(oDiff.changes.manual_picks.updated[0].skill_id, "pick/update");
  assert.equal(oDiff.changes.manual_picks.updated[0].before.reason, "旧原因");
  assert.equal(oDiff.changes.manual_picks.updated[0].after.reason, "新原因");

  const sDiff = computeSnoozedDiff(state);
  assert.equal(sDiff.changes.snoozed.added.length, 0);
  assert.equal(sDiff.changes.snoozed.removed.length, 0);
  assert.equal(sDiff.changes.snoozed.updated.length, 1);
  assert.equal(sDiff.changes.snoozed.updated[0].skill_id, "snooze/update");
  assert.equal(sDiff.changes.snoozed.updated[0].before.reason, "原原因");
  assert.equal(sDiff.changes.snoozed.updated[0].after.reason, "延长冷冻");
});

test("catalog-state: 8.1.4 新增后取消回到基线：净差异为零，浮条计数一致；基线取消后恢复按实际元数据判断", () => {
  const state = createOverridesState();
  state.baselinePicks["pick/exist"] = { skill_id: "pick/exist", reason: "基线项", added_at: "2026-09-01", from: "recommended" };

  // 1. 新增未在基线中的项，再取消 -> 净差异为 0
  togglePick(state, "pick/temp", "recommended", "2026-09-29");
  assert.equal(calculateChangesCount(state), 1);
  togglePick(state, "pick/temp", "recommended", "2026-09-29");
  assert.equal(calculateChangesCount(state), 0);
  const diff1 = computeOverridesDiff(state);
  assert.equal(diff1.summary.changed_records, 0);

  // 2. 基线条目取消后再恢复 -> 若元数据一致则为 0 变化
  togglePick(state, "pick/exist", "recommended", "2026-09-29");
  assert.equal(calculateChangesCount(state), 1);
  togglePick(state, "pick/exist", "recommended", "2026-09-29");
  assert.equal(calculateChangesCount(state), 0);
  const diff2 = computeOverridesDiff(state);
  assert.equal(diff2.summary.changed_records, 0);
});

test("catalog-state: 8.1.5 从 LocalStorage 恢复的旧变更仍显示为待同步；已经与当前基线一致的项不重复计数，不擅自清缓存", () => {
  const store = {};
  const mockStorage = {
    getItem: key => store[key] || null,
    setItem: (key, val) => { store[key] = String(val); },
    removeItem: key => { delete store[key]; }
  };

  const state1 = createOverridesState();
  state1.baselinePicks["pick/synced"] = { skill_id: "pick/synced", reason: "已合并到基线", added_at: "2026-09-01", from: "recommended" };
  state1.stagedPicks["pick/synced"] = { skill_id: "pick/synced", reason: "已合并到基线", added_at: "2026-09-01", from: "recommended" };
  state1.stagedPicks["pick/unsynced"] = { skill_id: "pick/unsynced", reason: "真正待同步", added_at: "2026-09-29", from: "recommended" };
  saveStorage(state1, "test_sync", mockStorage);

  const state2 = createOverridesState();
  state2.baselinePicks["pick/synced"] = { skill_id: "pick/synced", reason: "已合并到基线", added_at: "2026-09-01", from: "recommended" };
  loadStorage(state2, "test_sync", "legacy", mockStorage);

  const diff = computeOverridesDiff(state2);
  assert.equal(diff.summary.changed_records, 1);
  assert.equal(diff.changes.manual_picks.added.length, 1);
  assert.equal(diff.changes.manual_picks.added[0].skill_id, "pick/unsynced");
  assert.ok(store["test_sync"]);
});

test("catalog-state: 8.1.6 屏蔽转收藏、冷冻转收藏、收藏转冷冻的最终集合互斥，两个文件的关联修改齐全", () => {
  const state = createOverridesState();
  state.baselineExclusions["skill/block-to-fav"] = { skill_id: "skill/block-to-fav", reason: "原本屏蔽", added_at: "2026-09-01" };
  state.baselineSnoozed["skill/snooze-to-fav"] = { skill_id: "skill/snooze-to-fav", snoozed_at: "2026-09-01", expires_at: "2027-01-29", days: 150, reason: "原本冷冻" };
  state.baselinePicks["skill/fav-to-snooze"] = { skill_id: "skill/fav-to-snooze", reason: "原本收藏", added_at: "2026-09-01", from: "recommended" };

  // 1. 屏蔽转收藏
  togglePick(state, "skill/block-to-fav", "recommended", "2026-09-29");
  assert.equal(isPicked(state, "skill/block-to-fav"), true);
  assert.equal(isExcluded(state, "skill/block-to-fav"), false);
  const oDiff1 = computeOverridesDiff(state);
  assert.ok(oDiff1.changes.manual_picks.added.some(p => p.skill_id === "skill/block-to-fav"));
  assert.ok(oDiff1.changes.manual_exclusions.removed.some(p => p.skill_id === "skill/block-to-fav"));

  // 2. 冷冻转收藏（跨两个文件）
  togglePick(state, "skill/snooze-to-fav", "recommended", "2026-09-29");
  assert.equal(isPicked(state, "skill/snooze-to-fav"), true);
  assert.equal(isSnoozed(state, "skill/snooze-to-fav"), false);
  const oDiff2 = computeOverridesDiff(state);
  const sDiff2 = computeSnoozedDiff(state);
  assert.ok(oDiff2.changes.manual_picks.added.some(p => p.skill_id === "skill/snooze-to-fav"));
  assert.ok(sDiff2.changes.snoozed.removed.some(p => p.skill_id === "skill/snooze-to-fav"));

  // 3. 收藏转冷冻（跨两个文件）
  snoozeSkill(state, "skill/fav-to-snooze", "2026-09-29", 150);
  assert.equal(isPicked(state, "skill/fav-to-snooze"), false);
  assert.equal(isSnoozed(state, "skill/fav-to-snooze", "2026-09-29"), true);
  const oDiff3 = computeOverridesDiff(state);
  const sDiff3 = computeSnoozedDiff(state);
  assert.ok(oDiff3.changes.manual_picks.removed.some(p => p.skill_id === "skill/fav-to-snooze"));
  assert.ok(sDiff3.changes.snoozed.added.some(p => p.skill_id === "skill/fav-to-snooze"));

  assert.equal(detectConflicts(state).length, 0);
});

test("catalog-state: 冷冻互斥仅在活跃期生效，历史导出与净差异保持不变", () => {
  for (const collection of ["baselinePicks", "baselineExclusions"]) {
    const state = createOverridesState();
    const sid = "author/tool";
    state[collection][sid] = { skill_id: sid, reason: "已保存" };
    const snooze = { skill_id: sid, snoozed_at: "2026-01-01", expires_at: "2026-05-31", days: 150 };
    state.baselineSnoozed[sid] = snooze;
    const before = JSON.stringify(state);

    for (const today of ["2026-01-01", "2026-05-30"]) {
      assert.equal(detectConflicts(state, today).length, 1);
      assert.throws(() => generateOverridesJson(state, today), /跨集合状态冲突/);
      assert.throws(() => generateSnoozedJson(state, today), /跨集合状态冲突/);
    }
    for (const today of ["2025-12-31", "2026-05-31", "2026-09-29"]) {
      assert.deepEqual(detectConflicts(state, today), []);
      const full = JSON.parse(generateOverridesJson(state, today));
      assert.equal(full.manual_picks.length + full.manual_exclusions.length, 1);
      assert.deepEqual(JSON.parse(generateSnoozedJson(state, today)).snoozed, [snooze]);
    }
    assert.equal(computeSnoozedDiff(state).summary.changed_records, 0);
    assert.equal(JSON.stringify(state), before);
  }
});

test("catalog-state: 8.1.7 冷冻到期不会被差异生成器自动当作用户删除", () => {
  const state = createOverridesState();
  state.baselineSnoozed["expired/tool"] = {
    skill_id: "expired/tool",
    snoozed_at: "2026-01-01",
    expires_at: "2026-05-31",
    days: 150,
    reason: "用户暂不关注"
  };

  const diff = computeSnoozedDiff(state);
  assert.equal(diff.summary.changed_records, 0);
  assert.equal(diff.changes.snoozed.removed.length, 0);
  assert.equal(diff.changes.snoozed.added.length, 0);
  assert.equal(diff.changes.snoozed.updated.length, 0);

  const full = JSON.parse(generateSnoozedJson(state));
  assert.equal(full.snoozed.length, 1);
  assert.equal(full.snoozed[0].skill_id, "expired/tool");
});

test("catalog-state: 8.1.8 生成器输入不变；对象键顺序不影响比较，输出可重复；冲突不会导出为合法完整配置", () => {
  const state = createOverridesState();
  state.baselinePicks["tool/test"] = {
    skill_id: "tool/test",
    reason: "测试",
    added_at: "2026-09-01",
    from: "recommended"
  };
  state.stagedPicks["tool/test"] = {
    from: "recommended",
    added_at: "2026-09-01",
    reason: "测试",
    skill_id: "tool/test"
  };

  const diff = computeOverridesDiff(state);
  assert.equal(diff.summary.changed_records, 0);

  const json1 = generateIncrementalOverridesJson(state);
  const json2 = generateIncrementalOverridesJson(state);
  assert.equal(json1, json2);

  state.stagedExclusions["tool/test"] = { skill_id: "tool/test", reason: "恶意冲突", added_at: "2026-09-29" };
  const conflicts = detectConflicts(state);
  assert.equal(conflicts.length, 1);
  assert.equal(conflicts[0].skill_id, "tool/test");

  assert.throws(() => {
    generateOverridesJson(state);
  }, /跨集合状态冲突/);
});
