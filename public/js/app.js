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
  getEffectiveSnoozedList,
  isPicked
} from "./catalog-state.js?v=20260929_keywords_1";
import { renderCatalogList, renderDrawerContent, getCategoryIcon } from "./catalog-view.js?v=20260929_keywords_1";
import { renderFindView, fetchFindReport } from "./find-view.js?v=20260929_evidence_closed_1";
import { updateSyncBar, initSyncModal, initSnoozedModal, initConfirmModal } from "./modals.js?v=20260929_sync_2";
import {
  createOwnedState,
  loadOwnedStagedStorage,
  saveOwnedStagedStorage,
  loadOwnedPrivateStorage,
  populateOwnedBaseline,
  reconcileOwnedStaged,
  markOwned,
  unmarkOwned
} from "./owned-state.js?v=20260929_sync_2";
import { showToast } from "./owned-view.js?v=20260929_sync_2";
import { escapeHtml } from "./utils.js?v=20260929_sync_2";

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
  tabModalFavorites: document.getElementById("tab-modal-favorites"),
  tabModalOverrides: document.getElementById("tab-modal-overrides"),
  tabModalSnoozed: document.getElementById("tab-modal-snoozed"),
  tabModalOwned: document.getElementById("tab-modal-owned"),
  jsonPreview: document.getElementById("json-preview"),
  jsonViewModeLabel: document.getElementById("json-view-mode-label"),
  modalCrossWarning: document.getElementById("modal-cross-warning"),
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
  confirmModal: document.getElementById("confirm-modal"),
  confirmModalSkillName: document.getElementById("confirm-modal-skill-name"),
  btnCloseConfirmModal: document.getElementById("btn-close-confirm-modal"),
  btnCancelConfirm: document.getElementById("btn-cancel-confirm"),
  btnSubmitConfirm: document.getElementById("btn-submit-confirm"),
  btnThemeToggle: document.getElementById("btn-theme-toggle"),
  drawer: document.getElementById("detail-drawer"),
  drawerBackdrop: document.getElementById("drawer-backdrop"),
  drawerIcon: document.getElementById("drawer-icon"),
  drawerTitle: document.getElementById("drawer-title"),
  drawerBy: document.getElementById("drawer-by"),
  drawerBody: document.getElementById("drawer-body"),
  drawerActions: document.getElementById("drawer-actions"),
  drawerRepoLink: document.getElementById("drawer-repo-link"),
  btnCloseDrawer: document.getElementById("btn-close-drawer"),
  specialModal: document.getElementById("special-modal"),
  specialModalIcon: document.getElementById("special-modal-icon"),
  specialModalTitle: document.getElementById("special-modal-title"),
  specialModalList: document.getElementById("special-modal-list"),
  specialQ: document.getElementById("special-q"),
  btnCloseSpecialModal: document.getElementById("btn-close-special-modal"),
  btnCloseSpecialBottom: document.getElementById("btn-close-special-bottom"),
  btnSpecialMenu: document.getElementById("btn-special-menu"),
  specialDropdown: document.getElementById("special-dropdown"),
  itemSpecialStyle: document.getElementById("item-special-style"),
  itemSpecialProto: document.getElementById("item-special-proto")
};

/**
 * 刷新页面主列表与筛选统计。
 */
export function apply() {
  if (!state.data) return;

  if (state.tab === "find") {
    if (el.controls) el.controls.style.display = "none";
    if (state.findReport) {
      renderFindView(el.list, state.findReport, ownedState);
    } else {
      fetchFindReport().then(findData => {
        if (findData) {
          state.findReport = findData;
          if (el.badgeFind) {
            el.badgeFind.textContent = (findData.shortlist || []).length;
          }
          if (state.tab === "find") {
            renderFindView(el.list, state.findReport, ownedState);
          }
        }
      });
    }
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

  const shown = renderCatalogList(
    el.list,
    currentList,
    overridesState,
    state.tab,
    {
      q: state.q,
      category: state.category,
      source: state.source
    },
    {
      onBatchRendered: (rendered, total) => {
        el.meta.hidden = false;
        if (total > rendered) {
          el.meta.textContent = "本区共 " + currentList.length + " 条，当前显示 " + rendered + " 条（共 " + total + " 条匹配，点击底部可加载更多）。";
        } else {
          el.meta.textContent = "本区共 " + currentList.length + " 条，当前显示 " + shown + " 条。";
        }
      }
    }
  );

  el.meta.hidden = false;
  if (shown > 0 && shown <= 24) {
    el.meta.textContent = "本区共 " + currentList.length + " 条，当前显示 " + shown + " 条。";
  }

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

  // 尝试读取定向查找最新报告（优先命中内存缓存）
  fetchFindReport()
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

// 前端状态与同步边界说明：
// 本站部署于 GitHub Pages 静态环境，无服务端、无 WebSocket、不支持亦无需自动双向同步。
// 所有已收录标记、人工干预均在当前单页面内由 LocalStorage 暂存，业务同步模型为“本地暂存 -> 导出变更包/复制 JSON -> 手动合入仓库”。
// 单页面内的“标记、清空、导出”形成完整闭环，不维护复杂的跨标签页 Storage 广播监听。


// 全局键盘快捷键：随时按 '/' 聚焦搜索框；按 'Escape' 取消聚焦或关闭抽屉与弹窗
document.addEventListener("keydown", e => {
  if (e.key === "/" && document.activeElement !== el.q && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) {
    e.preventDefault();
    el.q?.focus();
    el.q?.select();
  } else if (e.key === "Escape") {
    if (document.activeElement === el.q) {
      el.q?.blur();
    } else if (el.specialDropdown && !el.specialDropdown.hidden) {
      if (typeof toggleSpecialMenu === "function") toggleSpecialMenu(false);
    } else if (el.drawer && el.drawer.classList.contains("is-open")) {
      closeDetailDrawer();
    } else if (el.specialModal && !el.specialModal.hidden) {
      closeSpecialModal();
    }
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

if (el.btnThemeToggle) {
  el.btnThemeToggle.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme") || "dark";
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("skill_theme", next);
    } catch (e) {}
  });
}

// 初始化确认弹窗控制器
const confirmModalController = initConfirmModal(el);

function executeMarkOwned(sid, skillName, sourceUrl, fromWhere, card) {
  markOwned(ownedState, {
    skill_id: sid,
    name: skillName,
    source_url: sourceUrl,
    original_partition: fromWhere
  });
  saveOwnedStagedStorage(ownedState);

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
}

// ==========================================================================
// 侧边详情抽屉控制器 (Detail Drawer)
// ==========================================================================
let currentDrawerSid = null;

function renderDrawerActions(entry) {
  if (!el.drawerActions || !entry) return;
  const sid = entry.skill_id;
  const picked = isPicked(overridesState, sid);
  const favBtnClass = picked ? "btn-action btn-fav is-active" : "btn-action btn-fav";
  const favBtnText = picked ? "★ 已收藏" : "★ 收藏";
  const ownedBtn = '<button type="button" class="btn-action btn-owned" data-action="owned" data-id="' + escapeHtml(sid) + '">标为已收录</button>';

  if (state.tab === "manual") {
    el.drawerActions.innerHTML =
      ownedBtn +
      '<button type="button" class="' + favBtnClass + '" data-action="fav" data-id="' + escapeHtml(sid) + '">' + favBtnText + '</button>' +
      '<button type="button" class="btn-action btn-block" data-action="block" data-id="' + escapeHtml(sid) + '">🚫 屏蔽</button>';
  } else {
    el.drawerActions.innerHTML =
      '<button type="button" class="' + favBtnClass + '" data-action="fav" data-id="' + escapeHtml(sid) + '">' + favBtnText + '</button>' +
      '<button type="button" class="btn-action btn-snooze" data-action="snooze" data-id="' + escapeHtml(sid) + '">⏳ 暂不看</button>' +
      '<button type="button" class="btn-action btn-block" data-action="block" data-id="' + escapeHtml(sid) + '">🚫 屏蔽</button>';
  }
}

export function openDetailDrawer(sid) {
  const entry = state.allEntries[sid];
  if (!entry || !el.drawer) return;
  currentDrawerSid = sid;

  const cat = entry.main_category ? entry.main_category.name : "未分类";
  const icon = getCategoryIcon(entry.main_category?.id, cat);

  if (el.drawerIcon) el.drawerIcon.textContent = icon;
  if (el.drawerTitle) el.drawerTitle.textContent = entry.name;
  if (el.drawerBy) {
    el.drawerBy.innerHTML = entry.author
      ? '<span class="author-label">by</span> <span class="author-name">@' + escapeHtml(entry.author) + '</span>'
      : '';
  }
  if (el.drawerBody) {
    el.drawerBody.innerHTML = renderDrawerContent(entry, overridesState, state.tab);
  }
  if (el.drawerRepoLink) {
    el.drawerRepoLink.href = entry.url || entry.repo_url || "#";
  }

  renderDrawerActions(entry);

  el.drawer.classList.add("is-open");
  el.drawer.setAttribute("aria-hidden", "false");
  document.body.classList.add("drawer-open");
}

export function closeDetailDrawer() {
  if (!el.drawer) return;
  el.drawer.classList.remove("is-open");
  el.drawer.setAttribute("aria-hidden", "true");
  document.body.classList.remove("drawer-open");
  currentDrawerSid = null;
}

if (el.btnCloseDrawer) el.btnCloseDrawer.addEventListener("click", closeDetailDrawer);
if (el.drawerBackdrop) el.drawerBackdrop.addEventListener("click", closeDetailDrawer);

if (el.drawerActions) {
  el.drawerActions.addEventListener("click", e => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const action = btn.getAttribute("data-action");
    const sid = btn.getAttribute("data-id") || currentDrawerSid;
    if (!sid) return;

    if (action === "fav") {
      togglePick(overridesState, sid, state.tab);
      saveStorage(overridesState);
      apply();
      const entry = state.allEntries[sid];
      if (entry) renderDrawerActions(entry);
    } else if (action === "block") {
      blockSkill(overridesState, sid);
      saveStorage(overridesState);
      closeDetailDrawer();
      apply();
    } else if (action === "snooze") {
      snoozeSkill(overridesState, sid);
      saveStorage(overridesState);
      closeDetailDrawer();
      apply();
    } else if (action === "owned") {
      const entry = state.allEntries[sid];
      const skillName = btn.getAttribute("data-name") || (entry && entry.name) || sid;
      const sourceUrl = btn.getAttribute("data-url") || (entry && (entry.url || entry.repo_url)) || null;
      const fromWhere = btn.getAttribute("data-from") || (entry && entry._baselineTab) || "manual";

      if (confirmModalController) {
        confirmModalController.open(skillName, () => {
          closeDetailDrawer();
          executeMarkOwned(sid, skillName, sourceUrl, fromWhere, null);
        });
      } else {
        closeDetailDrawer();
        executeMarkOwned(sid, skillName, sourceUrl, fromWhere, null);
      }
    }
  });
}

// 列表卡片按钮委托与抽屉联动
el.list.addEventListener("click", e => {
  const btn = e.target.closest("button[data-action]");
  if (btn) {
    const action = btn.getAttribute("data-action");
    const sid = btn.getAttribute("data-id");
    if (!sid) return;

    if (action === "open-detail") {
      openDetailDrawer(sid);
      return;
    }

    if (action === "fav") {
      togglePick(overridesState, sid, state.tab);
      saveStorage(overridesState);
      apply();
      if (currentDrawerSid === sid) {
        const entry = state.allEntries[sid];
        if (entry) renderDrawerActions(entry);
      }
    } else if (action === "block") {
      const card = btn.closest(".card");
      blockSkill(overridesState, sid);
      saveStorage(overridesState);
      if (currentDrawerSid === sid) closeDetailDrawer();
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
      if (currentDrawerSid === sid) closeDetailDrawer();
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
      const fromWhere = btn.getAttribute("data-from") || (entry && entry._baselineTab) || "manual";
      const card = btn.closest(".card");

      if (confirmModalController) {
        confirmModalController.open(skillName, () => {
          if (currentDrawerSid === sid) closeDetailDrawer();
          executeMarkOwned(sid, skillName, sourceUrl, fromWhere, card);
        });
      } else {
        if (currentDrawerSid === sid) closeDetailDrawer();
        executeMarkOwned(sid, skillName, sourceUrl, fromWhere, card);
      }
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
    }
    return;
  }

  // 忽略直接跳转外链或操作按钮区域的点击，卡片其余任意区域均展开详情抽屉
  if (e.target.closest('a[data-prevent-drawer="true"]') || e.target.closest(".card-actions")) {
    return;
  }

  const card = e.target.closest(".card");
  if (card && card.getAttribute("data-sid")) {
    openDetailDrawer(card.getAttribute("data-sid"));
  }
});

// ==========================================================================
// 特色专区画廊弹窗 (Special Skills Showcase Modal)
// ==========================================================================
const specialDataCache = {
  style: null,
  proto: null
};
let currentSpecialList = [];

function renderSpecialGallery(list) {
  if (!el.specialModalList) return;
  if (!list || !list.length) {
    el.specialModalList.innerHTML = '<div class="special-empty">没有匹配的条目</div>';
    return;
  }
  el.specialModalList.innerHTML = list.map(item => {
    const tagsHtml = (item.tags || []).slice(0, 4).map(t => '<span class="special-tag">' + escapeHtml(t) + '</span>').join('');
    const promptText = (item.example_requests && item.example_requests[0]) || item.summary_zh || '';
    return '<div class="special-card">' +
      '<div class="special-card-head">' +
        '<h4 class="special-card-title">' + escapeHtml(item.name) + '</h4>' +
        (item.author ? '<span class="special-card-author">@' + escapeHtml(item.author) + '</span>' : '') +
      '</div>' +
      '<p class="special-card-sum">' + escapeHtml(item.summary_zh || '') + '</p>' +
      (item.key_features && item.key_features.length ? '<p class="special-card-feature">✨ ' + escapeHtml(item.key_features[0]) + '</p>' : '') +
      '<div class="special-card-tags">' + tagsHtml + '</div>' +
      '<div class="special-card-footer">' +
        '<button type="button" class="btn-copy-prompt" data-prompt="' + escapeHtml(promptText) + '">📋 复制提示词</button>' +
        '<a href="' + escapeHtml(item.url || item.repo_url || '#') + '" target="_blank" rel="noopener noreferrer" class="special-link">仓库 ↗</a>' +
      '</div>' +
    '</div>';
  }).join('');
}

function openSpecialModal(type) {
  if (!el.specialModal) return;
  el.specialModal.hidden = false;
  if (el.specialQ) el.specialQ.value = "";

  if (type === "style") {
    if (el.specialModalIcon) el.specialModalIcon.textContent = "🎨";
    if (el.specialModalTitle) el.specialModalTitle.textContent = "UI 视觉设计风格库 (74 款)";
    if (specialDataCache.style) {
      currentSpecialList = specialDataCache.style.entries || [];
      renderSpecialGallery(currentSpecialList);
    } else {
      el.specialModalList.innerHTML = '<div class="special-loading">正在载入 UI 设计风格数据…</div>';
      fetch("data/special-skills/ui-design-style.json")
        .then(r => r.json())
        .then(data => {
          specialDataCache.style = data;
          currentSpecialList = data.entries || [];
          renderSpecialGallery(currentSpecialList);
        })
        .catch(err => {
          el.specialModalList.innerHTML = '<div class="special-empty">载入失败：' + escapeHtml(err.message) + '</div>';
        });
    }
  } else if (type === "proto") {
    if (el.specialModalIcon) el.specialModalIcon.textContent = "📐";
    if (el.specialModalTitle) el.specialModalTitle.textContent = "Web 交互原型与看板模板库";
    if (specialDataCache.proto) {
      currentSpecialList = specialDataCache.proto.entries || [];
      renderSpecialGallery(currentSpecialList);
    } else {
      el.specialModalList.innerHTML = '<div class="special-loading">正在载入 Web 原型模板数据…</div>';
      fetch("data/special-skills/web-prototype.json")
        .then(r => r.json())
        .then(data => {
          specialDataCache.proto = data;
          currentSpecialList = data.entries || [];
          renderSpecialGallery(currentSpecialList);
        })
        .catch(err => {
          el.specialModalList.innerHTML = '<div class="special-empty">载入失败：' + escapeHtml(err.message) + '</div>';
        });
    }
  }
}

function closeSpecialModal() {
  if (el.specialModal) el.specialModal.hidden = true;
}

if (el.specialQ) {
  el.specialQ.addEventListener("input", e => {
    const q = e.target.value.trim().toLowerCase();
    if (!q) {
      renderSpecialGallery(currentSpecialList);
      return;
    }
    const filtered = currentSpecialList.filter(item => {
      const haystack = [item.name, item.summary_zh, item.author]
        .concat(item.tags || [])
        .concat(item.example_requests || [])
        .concat(item.key_features || []);
      return haystack.some(h => h && String(h).toLowerCase().includes(q));
    });
    renderSpecialGallery(filtered);
  });
}

if (el.specialModalList) {
  el.specialModalList.addEventListener("click", e => {
    const btn = e.target.closest(".btn-copy-prompt");
    if (!btn) return;
    const prompt = btn.getAttribute("data-prompt");
    if (!prompt) return;
    navigator.clipboard.writeText(prompt).then(() => {
      const orig = btn.textContent;
      btn.textContent = "✓ 已复制！";
      setTimeout(() => { btn.textContent = orig; }, 1500);
    }).catch(() => {
      showToast("复制失败，请手动选择复制");
    });
  });
}

if (el.btnCloseSpecialModal) el.btnCloseSpecialModal.addEventListener("click", closeSpecialModal);
if (el.btnCloseSpecialBottom) el.btnCloseSpecialBottom.addEventListener("click", closeSpecialModal);

function toggleSpecialMenu(force) {
  if (!el.specialDropdown || !el.btnSpecialMenu) return;
  const isHidden = el.specialDropdown.hidden;
  const nextOpen = typeof force === "boolean" ? force : isHidden;
  el.specialDropdown.hidden = !nextOpen;
  el.btnSpecialMenu.classList.toggle("is-active", nextOpen);
  el.btnSpecialMenu.setAttribute("aria-expanded", nextOpen ? "true" : "false");
}

if (el.btnSpecialMenu) {
  el.btnSpecialMenu.addEventListener("click", e => {
    e.stopPropagation();
    toggleSpecialMenu();
  });
}

if (el.itemSpecialStyle) {
  el.itemSpecialStyle.addEventListener("click", () => {
    toggleSpecialMenu(false);
    openSpecialModal("style");
  });
}

if (el.itemSpecialProto) {
  el.itemSpecialProto.addEventListener("click", () => {
    toggleSpecialMenu(false);
    openSpecialModal("proto");
  });
}

// 点击空白区域自动收起特色专区下拉列表
document.addEventListener("click", e => {
  if (el.specialDropdown && !el.specialDropdown.hidden) {
    if (!e.target.closest(".special-menu-wrap")) {
      toggleSpecialMenu(false);
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
