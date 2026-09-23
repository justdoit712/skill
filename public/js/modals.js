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
import { renderSnoozedList } from "./catalog-view.js";

/**
 * 更新顶部未同步变更浮条。
 */
export function updateSyncBar(syncBarEl, syncSummaryEl, overridesState) {
  if (!syncBarEl || !syncSummaryEl) return;
  const pCount = Object.keys(overridesState.stagedPicks).length + overridesState.removedPicks.size;
  const sCount = Object.keys(overridesState.stagedSnoozed).length + overridesState.removedSnoozed.size;
  const eCount = Object.keys(overridesState.stagedExclusions).length + overridesState.removedExclusions.size;
  const total = pCount + sCount + eCount;

  if (total > 0) {
    syncBarEl.hidden = false;
    const parts = [];
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
export function initSyncModal(elements, overridesState, onClearSync) {
  let currentModalTab = "overrides";

  function updateModalContent() {
    elements.tabModalOverrides.classList.toggle("is-active", currentModalTab === "overrides");
    elements.tabModalSnoozed.classList.toggle("is-active", currentModalTab === "snoozed");

    if (currentModalTab === "overrides") {
      elements.modalTitle.textContent = "同步人工干预配置 (overrides.json)";
      elements.modalDesc.innerHTML =
        "本站部署于 GitHub Pages 静态环境。请将以下生成的<strong>收藏与屏蔽配置</strong>同步保存至仓库。点击下方绿色按钮将<strong>自动复制配置并直达 overrides.json 编辑页</strong>，粘贴提交即可生效！";
      elements.jsonPreview.textContent = generateOverridesJson(overridesState);
      elements.btnDownloadJson.textContent = "💾 下载 overrides.json";
      elements.btnGotoGithub.textContent = "🚀 复制并去 GitHub 保存 (overrides.json)";
    } else {
      elements.modalTitle.textContent = "同步暂不关注配置 (snoozed.json)";
      elements.modalDesc.innerHTML =
        "本站部署于 GitHub Pages 静态环境。请将以下生成的<strong>150 天冷冻配置</strong>同步保存至仓库。冷冻期内流水线零模型消耗跳过，到期当天自动恢复。点击下方绿色按钮将<strong>自动复制配置并直达 snoozed.json 编辑页</strong>，粘贴提交即可生效！";
      elements.jsonPreview.textContent = generateSnoozedJson(overridesState);
      elements.btnDownloadJson.textContent = "💾 下载 snoozed.json";
      elements.btnGotoGithub.textContent = "🚀 复制并去 GitHub 保存 (snoozed.json)";
    }
    elements.copyStatus.textContent = "";
  }

  function openSyncModal() {
    const pCount = Object.keys(overridesState.stagedPicks).length + overridesState.removedPicks.size;
    const eCount = Object.keys(overridesState.stagedExclusions).length + overridesState.removedExclusions.size;
    const sCount = Object.keys(overridesState.stagedSnoozed).length + overridesState.removedSnoozed.size;
    if (sCount > 0 && pCount === 0 && eCount === 0) {
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

  elements.btnOpenSync.addEventListener("click", openSyncModal);
  elements.btnCloseModal.addEventListener("click", closeSyncModal);
  elements.syncModal.addEventListener("click", e => {
    if (e.target === elements.syncModal) closeSyncModal();
  });

  elements.btnClearSync.addEventListener("click", () => {
    if (!confirm("确定要放弃所有未同步到仓库的本地修改吗？")) return;
    clearStorage(overridesState);
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
    const fileName = currentModalTab === "overrides" ? "overrides.json" : "snoozed.json";
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
    const jsonStr = elements.jsonPreview.textContent;
    const fileName = currentModalTab === "overrides" ? "overrides.json" : "snoozed.json";
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(jsonStr).catch(() => {});
    }
    elements.copyStatus.textContent = "🚀 已复制最新配置！正在打开 GitHub 在线编辑页…";
    window.open("https://github.com/justdoit712/skill/edit/main/config/" + fileName, "_blank");
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

  if (elements.btnOpenSnoozed) elements.btnOpenSnoozed.addEventListener("click", openSnoozedModal);
  if (elements.btnCloseSnoozedModal) elements.btnCloseSnoozedModal.addEventListener("click", closeSnoozedModal);
  if (elements.btnCloseSnoozedBottom) elements.btnCloseSnoozedBottom.addEventListener("click", closeSnoozedModal);
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
