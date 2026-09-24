/**
 * 技能索引站前端主控制器（单入口 Native ES Module）。
 * 负责 DOM 节点管理、数据加载、事件委托与视图刷新。
 */

import {
  createOverridesState,
  loadStorage,
  saveStorage,
  togglePick,
  blockSkill,
  snoozeSkill,
  populateBaseline,
  partitionEntries,
  getEffectiveSnoozedList
} from "./catalog-state.js";
import { renderCatalogList } from "./catalog-view.js";
import { renderFindView } from "./find-view.js";
import { updateSyncBar, initSyncModal, initSnoozedModal } from "./modals.js";
import {
  createOwnedState,
  loadOwnedStagedStorage,
  saveOwnedStagedStorage,
  loadOwnedPrivateStorage,
  populateOwnedBaseline,
  reconcileOwnedStaged,
  markOwned,
  unmarkOwned
} from "./owned-state.js";
import {
  showToast,
  initPrivateDetailsModal,
  initPrivateBackup
} from "./owned-view.js";

// 全局应用运行时状态
const state = {
  data: null,
  findReport: null,
  tab: "recommended",
  q: "",
  category: "",
  source: "",
  allEntries: {}
};

const overridesState = createOverridesState();
const ownedState = createOwnedState();

// DOM 元素缓存
const el = {
  list: document.getElementById("list"),
  status: document.getElementById("status"),
  meta: document.getElementById("meta"),
  empty: document.getElementById("empty"),
  emptyReason: document.getElementById("empty-reason"),
  q: document.getElementById("q"),
  category: document.getElementById("category"),
  source: document.getElementById("source"),
  tabRec: document.getElementById("tab-recommended"),
  tabCand: document.getElementById("tab-candidate"),
  tabManual: document.getElementById("tab-manual"),
  tabFind: document.getElementById("tab-find"),
  badgeRec: document.getElementById("count-recommended"),
  badgeCand: document.getElementById("count-candidate"),
  badgeManual: document.getElementById("count-manual"),
  badgeFind: document.getElementById("count-find"),
  controls: document.querySelector(".controls"),
  syncBar: document.getElementById("sync-bar"),
  syncSummary: document.getElementById("sync-summary"),
  btnOpenSync: document.getElementById("btn-open-sync"),
  btnClearSync: document.getElementById("btn-clear-sync"),
  syncModal: document.getElementById("sync-modal"),
  btnCloseModal: document.getElementById("btn-close-modal"),
  modalTitle: document.getElementById("modal-title"),
  modalDesc: document.getElementById("modal-desc"),
  tabModalOverrides: document.getElementById("tab-modal-overrides"),
  tabModalSnoozed: document.getElementById("tab-modal-snoozed"),
  tabModalOwned: document.getElementById("tab-modal-owned"),
  jsonPreview: document.getElementById("json-preview"),
  copyStatus: document.getElementById("copy-status"),
  btnCopyJson: document.getElementById("btn-copy-json"),
  btnDownloadJson: document.getElementById("btn-download-json"),
  btnGotoGithub: document.getElementById("btn-goto-github"),
  snoozedModal: document.getElementById("snoozed-modal"),
  btnCloseSnoozedModal: document.getElementById("btn-close-snoozed-modal"),
  btnCloseSnoozedBottom: document.getElementById("btn-close-snoozed-bottom"),
  btnOpenSnoozed: document.getElementById("btn-open-snoozed"),
  countSnoozedActive: document.getElementById("count-snoozed-active"),
  snoozedList: document.getElementById("snoozed-list"),
  privateModal: document.getElementById("private-modal"),
  btnClosePrivateModal: document.getElementById("btn-close-private-modal"),
  btnCancelPrivate: document.getElementById("btn-cancel-private"),
  btnSavePrivate: document.getElementById("btn-save-private"),
  btnDeletePrivate: document.getElementById("btn-delete-private"),
  privateSkillIdDisplay: document.getElementById("private-skill-id-display"),
  privateManagedUrl: document.getElementById("private-managed-url"),
  privateNote: document.getElementById("private-note"),
  privateUrlError: document.getElementById("private-url-error")
};

/**
 * 刷新页面主列表与筛选统计。
 */
export function apply() {
  if (!state.data) return;

  if (state.tab === "find") {
    if (el.controls) el.controls.style.display = "none";
    renderFindView(el.list, state.findReport, ownedState);
    el.meta.hidden = false;
    if (state.findReport && state.findReport.topic) {
      const slLen = (state.findReport.shortlist || []).length;
      const altLen = (state.findReport.alternatives || []).length;
      el.meta.textContent = "定向查找目标：『" + state.findReport.topic + "』· 优先推荐 " + slLen + " 项，相关备选 " + altLen + " 项。";
    } else {
      el.meta.textContent = "定向查找：暂无查找报告。";
    }
    updateSyncBar(el.syncBar, el.syncSummary, overridesState, ownedState);
    return;
  }

  if (el.controls) el.controls.style.display = "";

  const { activeRecommended, activeCandidates, activeManual } = partitionEntries(state.allEntries, overridesState, null, ownedState);

  if (el.badgeRec) el.badgeRec.textContent = activeRecommended.length;
  if (el.badgeCand) el.badgeCand.textContent = activeCandidates.length;
  if (el.badgeManual) el.badgeManual.textContent = activeManual.length;

  const currentList =
    state.tab === "recommended"
      ? activeRecommended
      : state.tab === "candidate"
      ? activeCandidates
      : activeManual;

  const shown = renderCatalogList(el.list, currentList, overridesState, state.tab, {
    q: state.q,
    category: state.category,
    source: state.source
  });

  el.meta.hidden = false;
  el.meta.textContent = "本区共 " + currentList.length + " 条，当前显示 " + shown + " 条。";

  const effectiveSnoozed = getEffectiveSnoozedList(overridesState);
  if (el.countSnoozedActive) {
    el.countSnoozedActive.textContent = effectiveSnoozed.length;
  }

  updateSyncBar(el.syncBar, el.syncSummary, overridesState, ownedState);
}

/**
 * 切换主 Tab。
 */
export function setTab(tab) {
  if (!["recommended", "candidate", "manual", "find"].includes(tab)) return;
  state.tab = tab;
  el.tabRec.classList.toggle("is-active", tab === "recommended");
  el.tabCand.classList.toggle("is-active", tab === "candidate");
  el.tabManual.classList.toggle("is-active", tab === "manual");
  if (el.tabFind) el.tabFind.classList.toggle("is-active", tab === "find");
  el.tabRec.setAttribute("aria-selected", tab === "recommended" ? "true" : "false");
  el.tabCand.setAttribute("aria-selected", tab === "candidate" ? "true" : "false");
  el.tabManual.setAttribute("aria-selected", tab === "manual" ? "true" : "false");
  if (el.tabFind) el.tabFind.setAttribute("aria-selected", tab === "find" ? "true" : "false");
  apply();
}

/**
 * 初始化数据加载成功后的回调。
 */
export function boot(data) {
  state.data = data;
  el.status.hidden = true;

  state.allEntries = populateBaseline(overridesState, data);
  populateOwnedBaseline(ownedState, data.owned, data.owned_entries);
  loadStorage(overridesState);
  loadOwnedStagedStorage(ownedState);
  loadOwnedPrivateStorage(ownedState);
  reconcileOwnedStaged(ownedState);
  saveOwnedStagedStorage(ownedState);

  (data.categories || []).forEach(c => {
    const opt = document.createElement("option");
    opt.value = c.id;
    opt.textContent = c.id + "（" + c.count + "）";
    el.category.appendChild(opt);
  });

  // 尝试读取定向查找最新报告
  fetch("data/find-report.json", { cache: "no-store" })
    .then(r => (r.ok ? r.json() : null))
    .then(findData => {
      if (findData) {
        state.findReport = findData;
        if (el.badgeFind) {
          el.badgeFind.textContent = (findData.shortlist || []).length;
        }
        if (state.tab === "find") {
          apply();
        }
      }
    })
    .catch(() => {});

  apply();
}

/**
 * 数据加载失败提示。
 */
export function fail(reason) {
  el.status.hidden = true;
  el.empty.hidden = false;
  el.emptyReason.textContent = reason;
}

// 多标签页并发状态同步（遵循 O-02）
window.addEventListener("storage", e => {
  if (e.key === "skills_catalog_owned_staged_v1" || e.key === "skills_catalog_owned_private_v1") {
    loadOwnedStagedStorage(ownedState);
    loadOwnedPrivateStorage(ownedState);
    reconcileOwnedStaged(ownedState);
    apply();
  } else if (e.key === "skills_catalog_overrides_v2" || e.key === "skill_overrides_v2") {
    loadStorage(overridesState);
    apply();
  }
});

// 事件监听与委托
el.q.addEventListener("input", e => {
  state.q = e.target.value.trim();
  apply();
});

el.category.addEventListener("change", e => {
  state.category = e.target.value;
  apply();
});

el.source.addEventListener("change", e => {
  state.source = e.target.value;
  apply();
});

el.tabRec.addEventListener("click", () => setTab("recommended"));
el.tabCand.addEventListener("click", () => setTab("candidate"));
el.tabManual.addEventListener("click", () => setTab("manual"));
if (el.tabFind) el.tabFind.addEventListener("click", () => setTab("find"));

// 初始化私人详情与备份控制器
const privateModalController = initPrivateDetailsModal(el, ownedState, () => apply());
initPrivateBackup(el, ownedState, () => apply());

// 列表卡片按钮委托
el.list.addEventListener("click", e => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const action = btn.getAttribute("data-action");
  const sid = btn.getAttribute("data-id");
  if (!sid) return;

  if (action === "fav") {
    togglePick(overridesState, sid, state.tab);
    saveStorage(overridesState);
    apply();
  } else if (action === "block") {
    const card = btn.closest(".card");
    blockSkill(overridesState, sid);
    saveStorage(overridesState);
    if (card) {
      card.classList.add("is-dismissing");
      setTimeout(() => apply(), 250);
    } else {
      apply();
    }
  } else if (action === "snooze") {
    const card = btn.closest(".card");
    snoozeSkill(overridesState, sid);
    saveStorage(overridesState);
    if (card) {
      card.classList.add("is-dismissing");
      setTimeout(() => apply(), 250);
    } else {
      apply();
    }
  } else if (action === "owned") {
    const entry = state.allEntries[sid];
    const skillName = btn.getAttribute("data-name") || (entry && entry.name) || sid;
    const sourceUrl = btn.getAttribute("data-url") || (entry && (entry.url || entry.repo_url)) || null;
    const fromWhere = btn.getAttribute("data-from") || (entry && entry._baselineTab) || (state.tab === "find" ? "find" : (state.tab || "candidate"));

    markOwned(ownedState, {
      skill_id: sid,
      name: skillName,
      source_url: sourceUrl,
      original_partition: fromWhere
    });
    saveOwnedStagedStorage(ownedState);

    const card = btn.closest(".card");
    if (card) {
      card.classList.add("is-dismissing");
      setTimeout(() => apply(), 250);
    } else {
      apply();
    }

    showToast("已将「" + skillName + "」标记为已收录", {
      onUndo: () => {
        unmarkOwned(ownedState, sid);
        saveOwnedStagedStorage(ownedState);
        apply();
      }
    });
  } else if (action === "unmark-owned") {
    unmarkOwned(ownedState, sid);
    saveOwnedStagedStorage(ownedState);
    const card = btn.closest(".card");
    if (card) {
      card.classList.add("is-dismissing");
      setTimeout(() => apply(), 250);
    } else {
      apply();
    }
    showToast("已取消已收录并按原分区规则恢复");
  } else if (action === "edit-private") {
    if (privateModalController) {
      privateModalController.open(sid);
    }
  }
});

// 初始化弹窗
initSyncModal(el, overridesState, () => apply(), ownedState);
initSnoozedModal(el, overridesState, () => state.allEntries, () => apply());

// 引导启动：读取目录数据
fetch("data/catalog.json", { cache: "no-store" })
  .then(r => {
    if (r.status === 404) throw new Error("目录数据尚未生成");
    if (!r.ok) throw new Error("读取目录数据失败（HTTP " + r.status + "）");
    return r.json();
  })
  .then(boot)
  .catch(err => {
    fail(err.message);
  });
