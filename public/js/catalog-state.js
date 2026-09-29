/**
 * 纯状态机：管理收藏（picks）、排除（exclusions）、冷冻（snoozed）状态变换、本地缓存与 JSON 生成。
 * 不依赖 DOM，完全可测试。
 */

import { shanghaiTodayStr, computeExpiresAt, isSnoozeActive } from "./utils.js?v=20260929_sync_2";
import { isOwned } from "./owned-state.js?v=20260929_sync_2";

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
 * 计算收藏（picks）的当前有效快照集合。
 */
export function computeEffectivePicks(overridesState) {
  const picks = Object.assign({}, overridesState.baselinePicks);
  overridesState.removedPicks.forEach(sid => {
    delete picks[sid];
  });
  Object.assign(picks, overridesState.stagedPicks);
  return picks;
}

/**
 * 计算排除（exclusions）的当前有效快照集合。
 */
export function computeEffectiveExclusions(overridesState) {
  const exclusions = Object.assign({}, overridesState.baselineExclusions);
  overridesState.removedExclusions.forEach(sid => {
    delete exclusions[sid];
  });
  Object.assign(exclusions, overridesState.stagedExclusions);
  return exclusions;
}

/**
 * 计算冷冻（snoozed）的当前有效快照集合（用于导出与净差异比对，不提前剔除过期项）。
 */
export function computeEffectiveSnoozed(overridesState) {
  const snoozed = Object.assign({}, overridesState.baselineSnoozed);
  overridesState.removedSnoozed.forEach(sid => {
    delete snoozed[sid];
  });
  Object.assign(snoozed, overridesState.stagedSnoozed);
  return snoozed;
}

/**
 * 检测跨集合冲突；仅活跃冷冻参与互斥，历史记录仍保留在导出集合中。
 */
export function detectConflicts(overridesState, today = null) {
  const curToday = today || shanghaiTodayStr();
  const effPicks = computeEffectivePicks(overridesState);
  const effExcl = computeEffectiveExclusions(overridesState);
  const effSnoozed = computeEffectiveSnoozed(overridesState);

  const allIds = new Set([
    ...Object.keys(effPicks),
    ...Object.keys(effExcl),
    ...Object.keys(effSnoozed)
  ]);

  const conflicts = [];
  allIds.forEach(sid => {
    const inCollections = [];
    if (effPicks[sid]) inCollections.push("manual_picks");
    if (effExcl[sid]) inCollections.push("manual_exclusions");
    if (isSnoozeActive(effSnoozed[sid], curToday)) inCollections.push("snoozed");

    if (inCollections.length > 1) {
      conflicts.push({
        skill_id: sid,
        collections: inCollections
      });
    }
  });

  return conflicts;
}

/**
 * 清洗对象，去除临时字段（以 _ 开头）并规范化结构用于深比较。
 */
export function cleanValue(val) {
  if (val === null || val === undefined || typeof val !== "object") {
    return val;
  }
  if (Array.isArray(val)) {
    return val.map(cleanValue);
  }
  const res = {};
  const keys = Object.keys(val).filter(k => !k.startsWith("_")).sort();
  for (const k of keys) {
    res[k] = cleanValue(val[k]);
  }
  return res;
}

/**
 * 键顺序无关、忽略以 _ 开头临时字段的纯深比较。
 */
export function isDeepEqual(a, b) {
  const cleanA = cleanValue(a);
  const cleanB = cleanValue(b);
  return JSON.stringify(cleanA) === JSON.stringify(cleanB);
}

/**
 * 对比基线与有效集合，生成结构化差异（added, updated, removed）。
 * 稳定按 skill_id 字母升序排序。
 */
export function diffCollection(baselineMap, effectiveMap) {
  const baseMap = baselineMap || {};
  const effMap = effectiveMap || {};
  const allIds = new Set([
    ...Object.keys(baseMap),
    ...Object.keys(effMap)
  ]);

  const added = [];
  const updated = [];
  const removed = [];

  Array.from(allIds).sort().forEach(sid => {
    const base = baseMap[sid];
    const eff = effMap[sid];

    if (eff && !base) {
      added.push(cleanValue(eff));
    } else if (base && !eff) {
      removed.push({
        skill_id: sid,
        before: cleanValue(base)
      });
    } else if (base && eff) {
      if (!isDeepEqual(base, eff)) {
        updated.push({
          skill_id: sid,
          before: cleanValue(base),
          after: cleanValue(eff)
        });
      }
    }
  });

  return { added, updated, removed };
}

/**
 * 计算 overrides.json 相对基线的结构化净差异。
 */
export function computeOverridesDiff(overridesState) {
  const effPicks = computeEffectivePicks(overridesState);
  const effExcl = computeEffectiveExclusions(overridesState);

  const picksDiff = diffCollection(overridesState.baselinePicks, effPicks);
  const exclDiff = diffCollection(overridesState.baselineExclusions, effExcl);

  const changedRecords =
    picksDiff.added.length +
    picksDiff.updated.length +
    picksDiff.removed.length +
    exclDiff.added.length +
    exclDiff.updated.length +
    exclDiff.removed.length;

  const affectedSkills = new Set();
  [...picksDiff.added, ...picksDiff.updated, ...picksDiff.removed].forEach(item => affectedSkills.add(item.skill_id));
  [...exclDiff.added, ...exclDiff.updated, ...exclDiff.removed].forEach(item => affectedSkills.add(item.skill_id));

  return {
    preview_version: "1.0.0",
    kind: "change_preview",
    target_file: "config/governance/overrides.json",
    notice: "仅供核对，不能覆盖配置文件，也不能直接导入",
    summary: {
      changed_records: changedRecords,
      affected_skills: affectedSkills.size
    },
    changes: {
      manual_picks: picksDiff,
      manual_exclusions: exclDiff
    }
  };
}

/**
 * 计算 snoozed.json 相对基线的结构化净差异。
 */
export function computeSnoozedDiff(overridesState) {
  const effSnoozed = computeEffectiveSnoozed(overridesState);
  const snoozedDiff = diffCollection(overridesState.baselineSnoozed, effSnoozed);

  const changedRecords =
    snoozedDiff.added.length +
    snoozedDiff.updated.length +
    snoozedDiff.removed.length;

  const affectedSkills = new Set();
  [...snoozedDiff.added, ...snoozedDiff.updated, ...snoozedDiff.removed].forEach(item => affectedSkills.add(item.skill_id));

  return {
    preview_version: "1.0.0",
    kind: "change_preview",
    target_file: "config/governance/snoozed.json",
    notice: "仅供核对，不能覆盖配置文件，也不能直接导入",
    summary: {
      changed_records: changedRecords,
      affected_skills: affectedSkills.size
    },
    changes: {
      snoozed: snoozedDiff
    }
  };
}

/**
 * 序列化输出待同步 overrides.json 增量只读核对 JSON。
 */
export function generateIncrementalOverridesJson(overridesState) {
  const diff = computeOverridesDiff(overridesState);
  return JSON.stringify(diff, null, 2);
}

/**
 * 序列化输出待同步 snoozed.json 增量只读核对 JSON。
 */
export function generateIncrementalSnoozedJson(overridesState) {
  const diff = computeSnoozedDiff(overridesState);
  return JSON.stringify(diff, null, 2);
}

/**
 * 获取当前所有生效的冷冻条目列表（已排除被收藏或被屏蔽的条目）。
 */
export function getEffectiveSnoozedList(overridesState, today = null) {
  const snoozedMap = computeEffectiveSnoozed(overridesState);
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

    // 收藏时清除排除状态（若在基线中则记录为移除，若在暂存中则删除暂存）
    delete overridesState.stagedExclusions[sid];
    if (overridesState.baselineExclusions[sid]) {
      overridesState.removedExclusions.add(sid);
    } else {
      overridesState.removedExclusions.delete(sid);
    }

    // 收藏时清除冷冻状态（若在基线中则记录为移除，若在暂存中则删除暂存）
    delete overridesState.stagedSnoozed[sid];
    if (overridesState.baselineSnoozed[sid]) {
      overridesState.removedSnoozed.add(sid);
    } else {
      overridesState.removedSnoozed.delete(sid);
    }

    if (overridesState.baselinePicks[sid]) {
      delete overridesState.stagedPicks[sid];
    } else {
      overridesState.stagedPicks[sid] = {
        skill_id: sid,
        reason: "用户收藏",
        added_at: curToday,
        from: currentTab
      };
    }
  }
}

/**
 * 屏蔽条目（屏蔽时自动清除收藏与冷冻状态）。
 */
export function blockSkill(overridesState, sid, today = null) {
  const curToday = today || shanghaiTodayStr();

  // 屏蔽时清除收藏
  delete overridesState.stagedPicks[sid];
  if (overridesState.baselinePicks[sid]) {
    overridesState.removedPicks.add(sid);
  } else {
    overridesState.removedPicks.delete(sid);
  }

  // 屏蔽时清除冷冻
  delete overridesState.stagedSnoozed[sid];
  if (overridesState.baselineSnoozed[sid]) {
    overridesState.removedSnoozed.add(sid);
  } else {
    overridesState.removedSnoozed.delete(sid);
  }

  // 添加到排除
  overridesState.removedExclusions.delete(sid);
  if (overridesState.baselineExclusions[sid]) {
    delete overridesState.stagedExclusions[sid];
  } else {
    overridesState.stagedExclusions[sid] = {
      skill_id: sid,
      reason: "用户屏蔽/删除",
      added_at: curToday
    };
  }
}

/**
 * 冷冻条目（冷冻时自动清除收藏与屏蔽状态）。
 */
export function snoozeSkill(overridesState, sid, today = null, days = 150) {
  const curToday = today || shanghaiTodayStr();
  const expAt = computeExpiresAt(curToday, days);

  // 冷冻时清除收藏和屏蔽，保持互斥
  delete overridesState.stagedPicks[sid];
  if (overridesState.baselinePicks[sid]) {
    overridesState.removedPicks.add(sid);
  } else {
    overridesState.removedPicks.delete(sid);
  }

  delete overridesState.stagedExclusions[sid];
  if (overridesState.baselineExclusions[sid]) {
    overridesState.removedExclusions.add(sid);
  } else {
    overridesState.removedExclusions.delete(sid);
  }

  overridesState.removedSnoozed.delete(sid);
  if (
    overridesState.baselineSnoozed[sid] &&
    overridesState.baselineSnoozed[sid].days === days &&
    overridesState.baselineSnoozed[sid].expires_at === expAt
  ) {
    delete overridesState.stagedSnoozed[sid];
  } else {
    overridesState.stagedSnoozed[sid] = {
      skill_id: sid,
      snoozed_at: curToday,
      expires_at: expAt,
      days: days,
      reason: "用户暂不关注"
    };
  }
}

/**
 * 取消冷冻条目。
 */
export function unsnoozeSkill(overridesState, sid) {
  delete overridesState.stagedSnoozed[sid];
  if (overridesState.baselineSnoozed[sid]) {
    overridesState.removedSnoozed.add(sid);
  } else {
    overridesState.removedSnoozed.delete(sid);
  }
}

/**
 * 计算未同步至仓库的修改条数（基于与基线的净差异记录数）。
 */
export function calculateChangesCount(overridesState) {
  const oDiff = computeOverridesDiff(overridesState);
  const sDiff = computeSnoozedDiff(overridesState);
  return oDiff.summary.changed_records + sDiff.summary.changed_records;
}

/**
 * 生成待同步的 overrides.json 文本。
 */
export function generateOverridesJson(overridesState, today = null) {
  const conflicts = detectConflicts(overridesState, today);
  if (conflicts.length > 0) {
    const list = conflicts.map(c => `${c.skill_id} (${c.collections.join(" + ")})`).join(", ");
    throw new Error(`检测到跨集合状态冲突：${list}，已阻止导出 overrides.json`);
  }
  const picks = computeEffectivePicks(overridesState);
  const exclusions = computeEffectiveExclusions(overridesState);

  const payload = {
    overrides_version: "1.0.0",
    source: "docs/运行说明.md §9",
    note: "人工干预名单（overrides）：由用户手工维护。manual_picks 长期保留在收藏区；manual_exclusions 为人工排除黑名单，流水线扫描到直接跳过（0 模型调用）。",
    manual_picks: Object.keys(picks).sort().map(k => cleanValue(picks[k])),
    manual_exclusions: Object.keys(exclusions).sort().map(k => cleanValue(exclusions[k]))
  };
  return JSON.stringify(payload, null, 2);
}

/**
 * 生成待同步的 snoozed.json 文本。
 */
export function generateSnoozedJson(overridesState, today = null) {
  const conflicts = detectConflicts(overridesState, today);
  if (conflicts.length > 0) {
    const list = conflicts.map(c => `${c.skill_id} (${c.collections.join(" + ")})`).join(", ");
    throw new Error(`检测到跨集合状态冲突：${list}，已阻止导出 snoozed.json`);
  }
  const snoozed = computeEffectiveSnoozed(overridesState);

  const payload = {
    snooze_version: "1.0.0",
    default_snooze_days: 150,
    source: "docs/运行说明.md §9.2",
    note: "临时不关注名单（snoozed）：由用户在网页端维护。在 snoozed_at <= today < expires_at 期间冷冻（默认150天），到期当天自动恢复显示与候选处理。流水线扫描时跳过模型调用，0 额外模型 token 消耗。",
    snoozed: Object.keys(snoozed).sort().map(k => cleanValue(snoozed[k]))
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
