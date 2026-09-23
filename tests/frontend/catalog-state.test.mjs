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
