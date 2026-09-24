/**
 * 纯状态机：管理已收录（Owned Skills）状态变换、基线与暂存变更包、私人详情与本地备份。
 * 遵循《已收录 Skill 管理：详细实施方案》§4.2 与 §4.4。
 * 不依赖 DOM，完全可测试。
 */

import { shanghaiTodayStr } from "./utils.js";

export const OWNED_SCHEMA_VERSION = "1.0.0";
export const STORAGE_KEY_OWNED_STAGED = "skills_catalog_owned_staged_v1";
export const STORAGE_KEY_OWNED_PRIVATE = "skills_catalog_owned_private_v1";

/**
 * 校验并规范化私人管理链接（只允许 HTTP(S)，拒绝脚本协议与用户名密码凭据）。
 */
export function validateManagedUrl(rawUrl) {
  if (!rawUrl || typeof rawUrl !== "string" || !rawUrl.trim()) {
    return "";
  }
  const trimmed = rawUrl.trim();
  let parsed;
  try {
    parsed = new URL(trimmed);
  } catch (e) {
    throw new Error("无效的管理链接 URL");
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error("管理链接仅支持 http:// 或 https:// 协议");
  }
  if (parsed.username || parsed.password) {
    throw new Error("管理链接不能包含用户名或密码凭据");
  }
  return parsed.href;
}

/**
 * 创建空白已收录状态对象。
 */
export function createOwnedState() {
  return {
    baseline: {},       // skill_id -> { skill_id, name, source_url, added_at, original_partition }
    stagedAdds: {},     // skill_id -> { skill_id, name, source_url, added_at, original_partition }
    stagedDeletes: new Set(), // skill_id
    privateDetails: {}  // skill_id -> { managed_url, note }
  };
}

/**
 * 从页面主数据载入已同步的已收录基线。
 */
export function populateOwnedBaseline(ownedState, ownedCatalogData, ownedEntriesData = []) {
  ownedState.baseline = {};
  const entriesMap = {};
  if (Array.isArray(ownedEntriesData)) {
    ownedEntriesData.forEach(entry => {
      if (entry && entry.skill_id) {
        entriesMap[entry.skill_id] = entry;
      }
    });
  }

  const items = Array.isArray(ownedCatalogData)
    ? ownedCatalogData
    : (ownedCatalogData && Array.isArray(ownedCatalogData.items))
      ? ownedCatalogData.items
      : [];

  items.forEach(item => {
    if (!item || !item.skill_id) return;
    const sid = item.skill_id;
    const entry = entriesMap[sid];
    ownedState.baseline[sid] = {
      skill_id: sid,
      name: item.name || (entry && entry.name) || sid,
      source_url: item.source_url || (entry && (entry.url || entry.repo_url)) || null,
      added_at: item.added_at || "",
      original_partition: (entry && entry.original_partition) || "candidate"
    };
  });
}

/**
 * 判断指定技能是否处于已收录状态。
 */
export function isOwned(ownedState, skillId) {
  if (!skillId) return false;
  if (ownedState.stagedDeletes.has(skillId)) return false;
  if (ownedState.stagedAdds[skillId]) return true;
  return Boolean(ownedState.baseline[skillId]);
}

/**
 * 标记为已收录。
 * 新增后立即取消应抵消该次新增，而不是产生一条虚假删除。
 */
export function markOwned(ownedState, item, today = null) {
  if (!item || !item.skill_id) return;
  const sid = item.skill_id;

  // 如果此前被标记了删除：取消删除标记
  if (ownedState.stagedDeletes.has(sid)) {
    ownedState.stagedDeletes.delete(sid);
    return;
  }

  // 如果已存在于基线，无需 stagedAdd
  if (ownedState.baseline[sid]) {
    return;
  }

  const curToday = today || shanghaiTodayStr();
  ownedState.stagedAdds[sid] = {
    skill_id: sid,
    name: item.name || sid,
    source_url: item.source_url || item.url || item.repo_url || null,
    added_at: curToday,
    original_partition: item.original_partition || item._baselineTab || "candidate"
  };
}

/**
 * 取消已收录状态。
 * 取消后保留私人详情（方便误操作恢复）。
 */
export function unmarkOwned(ownedState, skillId) {
  if (!skillId) return;

  // 若处于待新增暂存区：直接移除新增（抵消本次新增）
  if (ownedState.stagedAdds[skillId]) {
    delete ownedState.stagedAdds[skillId];
    return;
  }

  // 若存在于已同步基线：记录待删除
  if (ownedState.baseline[skillId]) {
    ownedState.stagedDeletes.add(skillId);
  }
}

/**
 * 计算待同步至仓库的变更总数。
 */
export function calculateOwnedChangesCount(ownedState) {
  return Object.keys(ownedState.stagedAdds).length + ownedState.stagedDeletes.size;
}

/**
 * 生成带前置条件的配置变更包（Patch JSON）。
 * 白名单安全：严禁输出任何 managed_url、note 等私人字段。
 */
export function generateOwnedPatch(ownedState) {
  const changes = [];

  // 1. 待新增条目 (before = null)
  Object.values(ownedState.stagedAdds).forEach(item => {
    changes.push({
      skill_id: item.skill_id,
      before: null,
      after: {
        skill_id: item.skill_id,
        name: item.name || item.skill_id,
        source_url: item.source_url || null,
        added_at: item.added_at
      }
    });
  });

  // 2. 待删除条目 (after = null)
  ownedState.stagedDeletes.forEach(sid => {
    const base = ownedState.baseline[sid];
    changes.push({
      skill_id: sid,
      before: base ? {
        skill_id: base.skill_id,
        name: base.name,
        source_url: base.source_url || null,
        added_at: base.added_at
      } : null,
      after: null
    });
  });

  return JSON.stringify({
    schema_version: OWNED_SCHEMA_VERSION,
    changes
  }, null, 2);
}

/**
 * 获取当前浏览器生效的已收录技能条目列表。
 */
export function getEffectiveOwnedList(ownedState, ownedEntriesMap = {}) {
  const resMap = {};

  // 1. 载入基线
  Object.keys(ownedState.baseline).forEach(sid => {
    if (!ownedState.stagedDeletes.has(sid)) {
      resMap[sid] = Object.assign({}, ownedState.baseline[sid]);
    }
  });

  // 2. 合并待新增
  Object.keys(ownedState.stagedAdds).forEach(sid => {
    resMap[sid] = Object.assign({}, ownedState.stagedAdds[sid]);
  });

  // 3. 关联 entries 快照与私人详情
  return Object.values(resMap).map(item => {
    const sid = item.skill_id;
    const entry = ownedEntriesMap[sid];
    const priv = ownedState.privateDetails[sid] || {};
    return {
      skill_id: sid,
      name: item.name || (entry && entry.name) || sid,
      source_url: item.source_url || (entry && (entry.url || entry.repo_url)) || null,
      added_at: item.added_at || "",
      original_partition: item.original_partition || (entry && entry.original_partition) || "candidate",
      entry: entry || null,
      managed_url: priv.managed_url || "",
      note: priv.note || ""
    };
  });
}

/**
 * 设置条目的私人详情（仅保存在当前浏览器，不参与变更包同步）。
 */
export function setPrivateDetails(ownedState, skillId, { managed_url = "", note = "" }) {
  if (!skillId) return;
  const cleanUrl = validateManagedUrl(managed_url);
  const cleanNote = typeof note === "string" ? note.trim() : "";

  if (!cleanUrl && !cleanNote) {
    delete ownedState.privateDetails[skillId];
  } else {
    ownedState.privateDetails[skillId] = {
      managed_url: cleanUrl,
      note: cleanNote
    };
  }
}

/**
 * 删除条目的私人详情。
 */
export function removePrivateDetails(ownedState, skillId) {
  if (!skillId) return;
  delete ownedState.privateDetails[skillId];
}

/**
 * 获取条目的私人详情。
 */
export function getPrivateDetails(ownedState, skillId) {
  if (!skillId) return { managed_url: "", note: "" };
  return ownedState.privateDetails[skillId] || { managed_url: "", note: "" };
}

/**
 * 导出私人详情备份 JSON 字符串。
 */
export function exportPrivateBackup(ownedState) {
  const cleanItems = {};
  Object.entries(ownedState.privateDetails).forEach(([sid, detail]) => {
    if (detail && (detail.managed_url || detail.note)) {
      cleanItems[sid] = {
        managed_url: detail.managed_url || "",
        note: detail.note || ""
      };
    }
  });

  return JSON.stringify({
    schema_version: OWNED_SCHEMA_VERSION,
    items: cleanItems
  }, null, 2);
}

/**
 * 导入私人详情备份并合并。
 * @param {object} ownedState
 * @param {string|object} backupData
 * @param {object} options
 * @param {'keep_local'|'use_imported'} options.conflictStrategy 遇到冲突时的策略，默认保留本地
 */
export function importPrivateBackup(ownedState, backupData, options = {}) {
  const strategy = options.conflictStrategy || "keep_local";
  const parsed = typeof backupData === "string" ? JSON.parse(backupData) : backupData;

  if (!parsed || typeof parsed !== "object") {
    throw new Error("私人备份数据格式无效");
  }
  if (parsed.schema_version !== OWNED_SCHEMA_VERSION) {
    throw new Error(`不支持的私人备份版本：${parsed.schema_version}`);
  }
  if (!parsed.items || typeof parsed.items !== "object") {
    throw new Error("私人备份缺少 items 字段");
  }

  let importedCount = 0;
  let conflictCount = 0;

  Object.entries(parsed.items).forEach(([sid, detail]) => {
    if (!sid || typeof detail !== "object") return;
    const cleanUrl = validateManagedUrl(detail.managed_url || "");
    const cleanNote = typeof detail.note === "string" ? detail.note.trim() : "";
    if (!cleanUrl && !cleanNote) return;

    const current = ownedState.privateDetails[sid];
    if (current && (current.managed_url !== cleanUrl || current.note !== cleanNote)) {
      conflictCount += 1;
      if (strategy === "use_imported") {
        ownedState.privateDetails[sid] = { managed_url: cleanUrl, note: cleanNote };
        importedCount += 1;
      }
    } else {
      ownedState.privateDetails[sid] = { managed_url: cleanUrl, note: cleanNote };
      importedCount += 1;
    }
  });

  return { success: true, importedCount, conflictCount };
}

/**
 * 持久化待同步变更至 LocalStorage。
 */
export function saveOwnedStagedStorage(ownedState, storageObj = null) {
  try {
    const storage = storageObj || (typeof localStorage !== "undefined" ? localStorage : null);
    if (!storage) return;
    const data = {
      stagedAdds: ownedState.stagedAdds,
      stagedDeletes: Array.from(ownedState.stagedDeletes)
    };
    storage.setItem(STORAGE_KEY_OWNED_STAGED, JSON.stringify(data));
  } catch (e) {}
}

/**
 * 从 LocalStorage 读取待同步变更。
 */
export function loadOwnedStagedStorage(ownedState, storageObj = null) {
  try {
    const storage = storageObj || (typeof localStorage !== "undefined" ? localStorage : null);
    if (!storage) return;
    const raw = storage.getItem(STORAGE_KEY_OWNED_STAGED);
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (saved.stagedAdds && typeof saved.stagedAdds === "object") {
      ownedState.stagedAdds = saved.stagedAdds;
    }
    if (Array.isArray(saved.stagedDeletes)) {
      ownedState.stagedDeletes = new Set(saved.stagedDeletes);
    }
  } catch (e) {}
}

/**
 * 清除已收录待同步变更缓存。
 */
export function clearOwnedStagedStorage(ownedState, storageObj = null) {
  try {
    const storage = storageObj || (typeof localStorage !== "undefined" ? localStorage : null);
    if (storage) {
      storage.removeItem(STORAGE_KEY_OWNED_STAGED);
    }
  } catch (e) {}
  ownedState.stagedAdds = {};
  ownedState.stagedDeletes.clear();
}

/**
 * 持久化私人详情至 LocalStorage。
 */
export function saveOwnedPrivateStorage(ownedState, storageObj = null) {
  try {
    const storage = storageObj || (typeof localStorage !== "undefined" ? localStorage : null);
    if (!storage) return;
    storage.setItem(STORAGE_KEY_OWNED_PRIVATE, JSON.stringify(ownedState.privateDetails));
  } catch (e) {}
}

/**
 * 从 LocalStorage 读取私人详情。
 */
export function loadOwnedPrivateStorage(ownedState, storageObj = null) {
  try {
    const storage = storageObj || (typeof localStorage !== "undefined" ? localStorage : null);
    if (!storage) return;
    const raw = storage.getItem(STORAGE_KEY_OWNED_PRIVATE);
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (saved && typeof saved === "object") {
      ownedState.privateDetails = saved;
    }
  } catch (e) {}
}
