/**
 * 纯状态机：管理收藏（picks）、排除（exclusions）、冷冻（snoozed）状态变换、本地缓存与 JSON 生成。
 * 不依赖 DOM，完全可测试。
 */

import { shanghaiTodayStr, computeExpiresAt, isSnoozeActive } from "./utils.js";
import { isOwned } from "./owned-state.js";

export const STORAGE_KEY = "skill_overrides_v2";
export const LEGACY_STORAGE_KEY = "skill_overrides_v1";

/**
 * 创建空白 overrides 状态对象。
 */
export function createOverridesState() {
  return {
    baselinePicks: {},
    baselineExclusions: {},
    stagedPicks: {},
    removedPicks: new Set(),
    stagedExclusions: {},
    removedExclusions: new Set(),
    baselineSnoozed: {},
    stagedSnoozed: {},
    removedSnoozed: new Set()
  };
}

/**
 * 判断技能是否处于收藏状态。
 */
export function isPicked(overridesState, skill_id) {
  if (overridesState.removedPicks.has(skill_id)) return false;
  if (overridesState.stagedPicks[skill_id]) return true;
  return Boolean(overridesState.baselinePicks[skill_id]);
}

/**
 * 判断技能是否处于排除（黑名单）状态。
 */
export function isExcluded(overridesState, skill_id) {
  if (overridesState.removedExclusions.has(skill_id)) return false;
  if (overridesState.stagedExclusions[skill_id]) return true;
  return Boolean(overridesState.baselineExclusions[skill_id]);
}

/**
 * 判断技能是否处于冷冻暂不关注状态。
 */
export function isSnoozed(overridesState, skill_id, today = null) {
  if (overridesState.removedSnoozed.has(skill_id)) return false;
  const item = overridesState.stagedSnoozed[skill_id] || overridesState.baselineSnoozed[skill_id];
  if (!item) return false;
  return isSnoozeActive(item, today);
}

/**
 * 获取当前所有生效的冷冻条目列表（已排除被收藏或被屏蔽的条目）。
 */
export function getEffectiveSnoozedList(overridesState, today = null) {
  const snoozedMap = Object.assign({}, overridesState.baselineSnoozed);
  overridesState.removedSnoozed.forEach(sid => {
    delete snoozedMap[sid];
  });
  Object.assign(snoozedMap, overridesState.stagedSnoozed);
  const curToday = today || shanghaiTodayStr();
  const res = [];
  Object.values(snoozedMap).forEach(item => {
    if (isSnoozeActive(item, curToday) && !isPicked(overridesState, item.skill_id) && !isExcluded(overridesState, item.skill_id)) {
      res.push(item);
    }
  });
  return res;
}

/**
 * 持久化状态至 LocalStorage。
 */
export function saveStorage(overridesState, storageKey = STORAGE_KEY, storageObj = null) {
  try {
    const storage = storageObj || (typeof localStorage !== "undefined" ? localStorage : null);
    if (!storage) return;
    const data = {
      stagedPicks: overridesState.stagedPicks,
      stagedExclusions: overridesState.stagedExclusions,
      removedPicks: Array.from(overridesState.removedPicks),
      removedExclusions: Array.from(overridesState.removedExclusions),
      stagedSnoozed: overridesState.stagedSnoozed,
      removedSnoozed: Array.from(overridesState.removedSnoozed)
    };
    storage.setItem(storageKey, JSON.stringify(data));
  } catch (e) {}
}

/**
 * 从 LocalStorage 读取状态。
 */
export function loadStorage(overridesState, storageKey = STORAGE_KEY, legacyKey = LEGACY_STORAGE_KEY, storageObj = null) {
  try {
    const storage = storageObj || (typeof localStorage !== "undefined" ? localStorage : null);
    if (!storage) return;
    const raw = storage.getItem(storageKey) || storage.getItem(legacyKey);
    if (!raw) {
      overridesState.stagedPicks = {};
      overridesState.stagedExclusions = {};
      overridesState.stagedSnoozed = {};
      overridesState.removedPicks = new Set();
      overridesState.removedExclusions = new Set();
      overridesState.removedSnoozed = new Set();
      return;
    }
    const saved = JSON.parse(raw);
    if (saved.stagedPicks) overridesState.stagedPicks = saved.stagedPicks;
    if (saved.stagedExclusions) overridesState.stagedExclusions = saved.stagedExclusions;
    if (saved.removedPicks) overridesState.removedPicks = new Set(saved.removedPicks);
    if (saved.removedExclusions) overridesState.removedExclusions = new Set(saved.removedExclusions);
    if (saved.stagedSnoozed) overridesState.stagedSnoozed = saved.stagedSnoozed;
    if (saved.removedSnoozed) overridesState.removedSnoozed = new Set(saved.removedSnoozed);
  } catch (e) {}
}

/**
 * 清除 LocalStorage 缓存与待同步暂存修改。
 */
export function clearStorage(overridesState, storageKey = STORAGE_KEY, legacyKey = LEGACY_STORAGE_KEY, storageObj = null) {
  try {
    const storage = storageObj || (typeof localStorage !== "undefined" ? localStorage : null);
    if (storage) {
      storage.removeItem(storageKey);
      storage.removeItem(legacyKey);
    }
  } catch (e) {}
  overridesState.stagedPicks = {};
  overridesState.stagedExclusions = {};
  overridesState.stagedSnoozed = {};
  overridesState.removedPicks.clear();
  overridesState.removedExclusions.clear();
  overridesState.removedSnoozed.clear();
}

/**
 * 切换收藏状态（收藏时清除冷冻与排除状态）。
 */
export function togglePick(overridesState, sid, currentTab = "recommended", today = null) {
  const curToday = today || shanghaiTodayStr();
  if (isPicked(overridesState, sid)) {
    delete overridesState.stagedPicks[sid];
    if (overridesState.baselinePicks[sid]) {
      overridesState.removedPicks.add(sid);
    }
  } else {
    overridesState.removedPicks.delete(sid);
    overridesState.removedExclusions.delete(sid);
    delete overridesState.stagedExclusions[sid];
    // 收藏时自动清除冷冻状态
    delete overridesState.stagedSnoozed[sid];
    if (overridesState.baselineSnoozed[sid]) {
      overridesState.removedSnoozed.add(sid);
    }
    overridesState.stagedPicks[sid] = {
      skill_id: sid,
      reason: "用户收藏",
      added_at: curToday,
      from: currentTab
    };
  }
}

/**
 * 屏蔽条目（屏蔽时自动清除收藏与冷冻状态）。
 */
export function blockSkill(overridesState, sid, today = null) {
  const curToday = today || shanghaiTodayStr();
  overridesState.removedPicks.delete(sid);
  delete overridesState.stagedPicks[sid];
  if (overridesState.baselinePicks[sid]) {
    overridesState.removedPicks.add(sid);
  }
  // 屏蔽时自动清除冷冻状态
  delete overridesState.stagedSnoozed[sid];
  if (overridesState.baselineSnoozed[sid]) {
    overridesState.removedSnoozed.add(sid);
  }
  overridesState.removedExclusions.delete(sid);
  overridesState.stagedExclusions[sid] = {
    skill_id: sid,
    reason: "用户屏蔽/删除",
    added_at: curToday
  };
}

/**
 * 冷冻条目（冷冻时自动清除收藏与屏蔽状态）。
 */
export function snoozeSkill(overridesState, sid, today = null, days = 150) {
  const curToday = today || shanghaiTodayStr();
  const expAt = computeExpiresAt(curToday, days);

  // 冷冻时清除收藏和屏蔽，保持互斥
  overridesState.removedPicks.delete(sid);
  delete overridesState.stagedPicks[sid];
  if (overridesState.baselinePicks[sid]) {
    overridesState.removedPicks.add(sid);
  }
  overridesState.removedExclusions.delete(sid);
  delete overridesState.stagedExclusions[sid];
  if (overridesState.baselineExclusions[sid]) {
    overridesState.removedExclusions.add(sid);
  }

  overridesState.removedSnoozed.delete(sid);
  overridesState.stagedSnoozed[sid] = {
    skill_id: sid,
    snoozed_at: curToday,
    expires_at: expAt,
    days: days,
    reason: "用户暂不关注"
  };
}

/**
 * 取消冷冻条目。
 */
export function unsnoozeSkill(overridesState, sid) {
  delete overridesState.stagedSnoozed[sid];
  if (overridesState.baselineSnoozed[sid]) {
    overridesState.removedSnoozed.add(sid);
  }
}

/**
 * 计算未同步至仓库的修改条数。
 */
export function calculateChangesCount(overridesState) {
  return (
    Object.keys(overridesState.stagedPicks).length +
    overridesState.removedPicks.size +
    Object.keys(overridesState.stagedExclusions).length +
    overridesState.removedExclusions.size +
    Object.keys(overridesState.stagedSnoozed).length +
    overridesState.removedSnoozed.size
  );
}

/**
 * 生成待同步的 overrides.json 文本。
 */
export function generateOverridesJson(overridesState) {
  const picks = Object.assign({}, overridesState.baselinePicks);
  overridesState.removedPicks.forEach(sid => {
    delete picks[sid];
  });
  Object.assign(picks, overridesState.stagedPicks);

  const exclusions = Object.assign({}, overridesState.baselineExclusions);
  overridesState.removedExclusions.forEach(sid => {
    delete exclusions[sid];
  });
  Object.assign(exclusions, overridesState.stagedExclusions);

  const payload = {
    overrides_version: "1.0.0",
    source: "docs/运行说明.md §9",
    note: "人工干预名单（overrides）：由用户手工维护。manual_picks 长期保留在收藏区；manual_exclusions 为人工排除黑名单，流水线扫描到直接跳过（0 模型调用）。",
    manual_picks: Object.values(picks),
    manual_exclusions: Object.values(exclusions)
  };
  return JSON.stringify(payload, null, 2);
}

/**
 * 生成待同步的 snoozed.json 文本。
 */
export function generateSnoozedJson(overridesState) {
  const snoozed = Object.assign({}, overridesState.baselineSnoozed);
  overridesState.removedSnoozed.forEach(sid => {
    delete snoozed[sid];
  });
  Object.assign(snoozed, overridesState.stagedSnoozed);

  const payload = {
    snooze_version: "1.0.0",
    default_snooze_days: 150,
    source: "docs/运行说明.md §9.2",
    note: "临时不关注名单（snoozed）：由用户在网页端维护。在 snoozed_at <= today < expires_at 期间冷冻（默认150天），到期当天自动恢复显示与候选处理。流水线扫描时跳过模型调用，0 额外模型 token 消耗。",
    snoozed: Object.values(snoozed)
  };
  return JSON.stringify(payload, null, 2);
}

/**
 * 基于 catalog.json 数据填充基线状态。
 */
export function populateBaseline(overridesState, data, today = null) {
  const curToday = today || shanghaiTodayStr();
  (data.manual || []).forEach(e => {
    overridesState.baselinePicks[e.skill_id] = {
      skill_id: e.skill_id,
      reason: (e.manual_note && e.manual_note.reason) || "人工收藏",
      added_at: (e.manual_note && e.manual_note.added_at) || curToday,
      from: (e.manual_note && e.manual_note.from) || "recommended"
    };
  });

  if (data.overrides) {
    (data.overrides.manual_picks || []).forEach(p => {
      if (p && p.skill_id) overridesState.baselinePicks[p.skill_id] = p;
    });
    (data.overrides.manual_exclusions || []).forEach(ex => {
      if (ex && ex.skill_id) overridesState.baselineExclusions[ex.skill_id] = ex;
    });
  }

  if (data.snoozed) {
    const sList = Array.isArray(data.snoozed) ? data.snoozed : (data.snoozed.snoozed || []);
    sList.forEach(sn => {
      if (sn && sn.skill_id) overridesState.baselineSnoozed[sn.skill_id] = sn;
    });
  }

  const allEntries = {};
  (data.recommended || []).forEach(e => {
    e._baselineTab = "recommended";
    allEntries[e.skill_id] = e;
  });
  (data.candidates || []).forEach(e => {
    e._baselineTab = "candidate";
    allEntries[e.skill_id] = e;
  });
  (data.manual || []).forEach(e => {
    e._baselineTab = "manual";
    allEntries[e.skill_id] = e;
  });
  (data.owned_entries || []).forEach(e => {
    e._baselineTab = e.original_partition || (e.status === "recommended" ? "recommended" : (e.status === "candidate" ? "candidate" : (e.status || "unknown")));
    allEntries[e.skill_id] = e;
  });

  Object.values(allEntries).forEach(e => {
    if (e.snooze && e.snooze.expires_at) {
      if (!overridesState.baselineSnoozed[e.skill_id]) {
        overridesState.baselineSnoozed[e.skill_id] = e.snooze;
      }
    }
  });

  return allEntries;
}

/**
 * 分区条目：已收录项排除，排除项跳过，收藏项进入 activeManual，冷冻项跳过，其余按基线严格白名单划入 activeRecommended / activeCandidates。
 * 遵循《已收录功能代码复核与修复方案》O-03：非展示状态（excluded, pending, processing_failure 等）决不误入候选区。
 */
export function partitionEntries(allEntries, overridesState, today = null, ownedState = null) {
  const curToday = today || shanghaiTodayStr();
  const activeManual = [];
  const activeRecommended = [];
  const activeCandidates = [];

  Object.keys(allEntries).forEach(sid => {
    const entry = allEntries[sid];
    if (ownedState && isOwned(ownedState, sid)) {
      return;
    }
    if (isExcluded(overridesState, sid)) {
      return;
    }
    if (isPicked(overridesState, sid)) {
      activeManual.push(entry);
    } else {
      if (isSnoozed(overridesState, sid, curToday)) {
        return;
      }

      // 严格白名单过滤：排除项、待处理项、失败项不进入推荐或候选
      const isAutoExcluded = entry.status === "excluded" || entry._baselineTab === "excluded";
      const isPendingOrFailed = entry.status === "pending" || entry.status === "processing_failure" ||
                                entry._baselineTab === "pending" || entry._baselineTab === "processing_failure";
      if (isAutoExcluded || isPendingOrFailed) {
        return;
      }

      if (entry._baselineTab === "recommended" || entry.status === "recommended") {
        activeRecommended.push(entry);
      } else if (entry._baselineTab === "candidate" || entry.status === "candidate") {
        activeCandidates.push(entry);
      }
    }
  });

  return { activeRecommended, activeCandidates, activeManual };
}
