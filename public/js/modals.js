/**
 * 弹窗控制器模块：同步配置弹窗 (syncModal) 与冷冻条目弹窗 (snoozedModal)。
 */

import {
  generateOverridesJson,
  generateSnoozedJson,
  clearStorage,
  unsnoozeSkill,
  getEffectiveSnoozedList,
  saveStorage
} from "./catalog-state.js";
import {
  generateOwnedPatch,
  calculateOwnedChangesCount,
  clearOwnedStagedStorage
} from "./owned-state.js";
import { renderSnoozedList } from "./catalog-view.js";

/**
 * 更新顶部未同步变更浮条。
 */
export function updateSyncBar(syncBarEl, syncSummaryEl, overridesState, ownedState = null) {
  if (!syncBarEl || !syncSummaryEl) return;
  const pCount = Object.keys(overridesState.stagedPicks).length + overridesState.removedPicks.size;
  const sCount = Object.keys(overridesState.stagedSnoozed).length + overridesState.removedSnoozed.size;
  const eCount = Object.keys(overridesState.stagedExclusions).length + overridesState.removedExclusions.size;
  const oCount = ownedState ? calculateOwnedChangesCount(ownedState) : 0;
  const total = pCount + sCount + eCount + oCount;

  if (total > 0) {
    syncBarEl.hidden = false;
    const parts = [];
    if (oCount > 0) parts.push("已收录 " + oCount);
    if (pCount > 0) parts.push("收藏 " + pCount);
    if (sCount > 0) parts.push("暂不看 " + sCount);
    if (eCount > 0) parts.push("屏蔽 " + eCount);
    syncSummaryEl.textContent = parts.join(" · ") || (total + " 项");
  } else {
    syncBarEl.hidden = true;
  }
}

/**
 * 初始化同步配置弹窗事件绑定与交互。
 */
export function initSyncModal(elements, overridesState, onClearSync, ownedState = null) {
  let currentModalTab = "overrides";

  function updateModalContent() {
    elements.tabModalOverrides.classList.toggle("is-active", currentModalTab === "overrides");
    elements.tabModalSnoozed.classList.toggle("is-active", currentModalTab === "snoozed");
    if (elements.tabModalOwned) {
      elements.tabModalOwned.classList.toggle("is-active", currentModalTab === "owned");
    }

    if (currentModalTab === "overrides") {
      elements.modalTitle.textContent = "同步人工干预配置 (overrides.json)";
      elements.modalDesc.innerHTML =
        "本站部署于 GitHub Pages 静态环境。请将以下生成的<strong>收藏与屏蔽配置</strong>同步保存至仓库。点击下方绿色按钮将<strong>自动复制配置并直达 overrides.json 编辑页</strong>，粘贴提交即可生效！";
      elements.jsonPreview.textContent = generateOverridesJson(overridesState);
      elements.btnDownloadJson.textContent = "💾 下载 overrides.json";
      elements.btnGotoGithub.textContent = "🚀 复制并去 GitHub 保存 (overrides.json)";
    } else if (currentModalTab === "snoozed") {
      elements.modalTitle.textContent = "同步暂不关注配置 (snoozed.json)";
      elements.modalDesc.innerHTML =
        "本站部署于 GitHub Pages 静态环境。请将以下生成的<strong>150 天冷冻配置</strong>同步保存至仓库。冷冻期内流水线零模型消耗跳过，到期当天自动恢复。点击下方绿色按钮将<strong>自动复制配置并直达 snoozed.json 编辑页</strong>，粘贴提交即可生效！";
      elements.jsonPreview.textContent = generateSnoozedJson(overridesState);
      elements.btnDownloadJson.textContent = "💾 下载 snoozed.json";
      elements.btnGotoGithub.textContent = "🚀 复制并去 GitHub 保存 (snoozed.json)";
    } else {
      elements.modalTitle.textContent = "同步已收录变更包 (owned-patch.json)";
      elements.modalDesc.innerHTML =
        "本站部署于 GitHub Pages 静态环境。请下载下方生成的<strong>带前置条件的已收录变更包</strong>并在本地执行 <code>python tools/manage_owned.py --apply-changes owned-patch.json</code> 合并配置并刷新页面数据，随后提交并推送 <code>config/owned-skills.json</code>。Actions 不会自动合并裸变更包。";
      elements.jsonPreview.textContent = ownedState ? generateOwnedPatch(ownedState) : "{}";
      elements.btnDownloadJson.textContent = "💾 下载 owned-patch.json";
      elements.btnGotoGithub.textContent = "📋 复制本地合并命令";
    }
    elements.copyStatus.textContent = "";
  }

  function openSyncModal() {
    const pCount = Object.keys(overridesState.stagedPicks).length + overridesState.removedPicks.size;
    const eCount = Object.keys(overridesState.stagedExclusions).length + overridesState.removedExclusions.size;
    const sCount = Object.keys(overridesState.stagedSnoozed).length + overridesState.removedSnoozed.size;
    const oCount = ownedState ? calculateOwnedChangesCount(ownedState) : 0;

    if (oCount > 0 && pCount === 0 && eCount === 0 && sCount === 0) {
      currentModalTab = "owned";
    } else if (sCount > 0 && pCount === 0 && eCount === 0) {
      currentModalTab = "snoozed";
    } else {
      currentModalTab = "overrides";
    }
    updateModalContent();
    elements.syncModal.hidden = false;
  }

  function closeSyncModal() {
    elements.syncModal.hidden = true;
  }

  elements.tabModalOverrides.addEventListener("click", () => {
    currentModalTab = "overrides";
    updateModalContent();
  });

  elements.tabModalSnoozed.addEventListener("click", () => {
    currentModalTab = "snoozed";
    updateModalContent();
  });

  if (elements.tabModalOwned) {
    elements.tabModalOwned.addEventListener("click", () => {
      currentModalTab = "owned";
      updateModalContent();
    });
  }

  elements.btnOpenSync.addEventListener("click", openSyncModal);
  elements.btnCloseModal.addEventListener("click", closeSyncModal);
  elements.syncModal.addEventListener("click", e => {
    if (e.target === elements.syncModal) closeSyncModal();
  });

  elements.btnClearSync.addEventListener("click", () => {
    if (!confirm("确定要放弃所有未同步到仓库的本地修改吗？")) return;
    clearStorage(overridesState);
    if (ownedState) clearOwnedStagedStorage(ownedState);
    if (onClearSync) onClearSync();
  });

  elements.btnCopyJson.addEventListener("click", () => {
    const jsonStr = elements.jsonPreview.textContent;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(jsonStr).then(() => {
        elements.copyStatus.textContent = "✅ 已成功复制 JSON 到剪贴板！";
      });
    } else {
      elements.copyStatus.textContent = "请直接全选上方文本框进行复制。";
    }
  });

  elements.btnDownloadJson.addEventListener("click", () => {
    const jsonStr = elements.jsonPreview.textContent;
    const fileName =
      currentModalTab === "overrides"
        ? "overrides.json"
        : currentModalTab === "snoozed"
        ? "snoozed.json"
        : "owned-patch.json";
    const blob = new Blob([jsonStr], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = fileName;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    elements.copyStatus.textContent = "✅ 已下载 " + fileName + " 文件。";
  });

  elements.btnGotoGithub.addEventListener("click", () => {
    if (currentModalTab === "owned") {
      const cmd = "python tools/manage_owned.py --apply-changes owned-patch.json";
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(cmd).catch(() => {});
      }
      elements.copyStatus.textContent = "📋 已复制本地合并命令：" + cmd;
      return;
    }

    const jsonStr = elements.jsonPreview.textContent;
    const fileName = currentModalTab === "overrides" ? "overrides.json" : "snoozed.json";
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(jsonStr).catch(() => {});
    }
    elements.copyStatus.textContent = "🚀 已复制最新配置！正在打开 GitHub 在线编辑页…";
    window.open("https://github.com/justdoit712/skill/edit/main/config/governance/" + fileName, "_blank");
  });

  return { openSyncModal, closeSyncModal, updateModalContent };
}

/**
 * 初始化冷冻弹窗事件绑定与交互。
 */
export function initSnoozedModal(elements, overridesState, getAllEntries, onUnsnooze) {
  function refreshList() {
    const list = getEffectiveSnoozedList(overridesState);
    renderSnoozedList(elements.snoozedList, list, getAllEntries());
  }

  function openSnoozedModal() {
    refreshList();
    elements.snoozedModal.hidden = false;
  }

  function closeSnoozedModal() {
    elements.snoozedModal.hidden = true;
  }

  if (elements.btnOpenSnoozed) {
    elements.btnOpenSnoozed.addEventListener("click", openSnoozedModal);
  }
  if (elements.btnCloseSnoozedModal) {
    elements.btnCloseSnoozedModal.addEventListener("click", closeSnoozedModal);
  }
  if (elements.btnCloseSnoozedBottom) {
    elements.btnCloseSnoozedBottom.addEventListener("click", closeSnoozedModal);
  }
  if (elements.snoozedModal) {
    elements.snoozedModal.addEventListener("click", e => {
      if (e.target === elements.snoozedModal) closeSnoozedModal();
    });
  }

  if (elements.snoozedList) {
    elements.snoozedList.addEventListener("click", e => {
      const btn = e.target.closest("button[data-action='unsnooze']");
      if (!btn) return;
      const sid = btn.getAttribute("data-id");
      if (!sid) return;

      unsnoozeSkill(overridesState, sid);
      saveStorage(overridesState);
      refreshList();
      if (onUnsnooze) onUnsnooze();
    });
  }

  return { openSnoozedModal, closeSnoozedModal, refreshList };
}

/**
 * 初始化候选区二次确认弹窗控制器。
 */
export function initConfirmModal(elements) {
  let pendingConfirmCallback = null;

  function open(skillName, onConfirm) {
    if (!elements.confirmModal) {
      if (onConfirm) onConfirm();
      return;
    }
    pendingConfirmCallback = onConfirm;
    if (elements.confirmModalSkillName) {
      elements.confirmModalSkillName.textContent = skillName || "";
    }
    elements.confirmModal.hidden = false;
  }

  function close() {
    if (elements.confirmModal) {
      elements.confirmModal.hidden = true;
    }
    pendingConfirmCallback = null;
  }

  if (elements.btnCancelConfirm) {
    elements.btnCancelConfirm.addEventListener("click", close);
  }
  if (elements.btnCloseConfirmModal) {
    elements.btnCloseConfirmModal.addEventListener("click", close);
  }
  if (elements.btnSubmitConfirm) {
    elements.btnSubmitConfirm.addEventListener("click", () => {
      const cb = pendingConfirmCallback;
      close();
      if (cb) cb();
    });
  }
  if (elements.confirmModal) {
    elements.confirmModal.addEventListener("click", e => {
      if (e.target === elements.confirmModal) close();
    });
  }
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && elements.confirmModal && !elements.confirmModal.hidden) {
      close();
    }
  });

  return { open, close };
}
