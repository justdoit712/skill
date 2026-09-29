import {
  generateIncrementalOverridesJson,
  generateIncrementalSnoozedJson,
  computeOverridesDiff,
  computeSnoozedDiff,
  detectConflicts,
  clearStorage,
  unsnoozeSkill,
  getEffectiveSnoozedList,
  saveStorage
} from "./catalog-state.js?v=20260929_sync_3";
import {
  generateOwnedPatch,
  calculateOwnedChangesCount,
  clearOwnedStagedStorage
} from "./owned-state.js?v=20260929_sync_3";
import { renderSnoozedList } from "./catalog-view.js?v=20260929_sync_3";
import { escapeHtml } from "./utils.js?v=20260929_sync_3";

/**
 * 更新顶部未同步变更浮条（基于净变化统计）。
 */
export function updateSyncBar(syncBarEl, syncSummaryEl, overridesState, ownedState = null) {
  if (!syncBarEl || !syncSummaryEl) return;
  const oDiff = computeOverridesDiff(overridesState);
  const sDiff = computeSnoozedDiff(overridesState);
  const pCount =
    oDiff.changes.manual_picks.added.length +
    oDiff.changes.manual_picks.updated.length +
    oDiff.changes.manual_picks.removed.length;
  const eCount =
    oDiff.changes.manual_exclusions.added.length +
    oDiff.changes.manual_exclusions.updated.length +
    oDiff.changes.manual_exclusions.removed.length;
  const sCount =
    sDiff.changes.snoozed.added.length +
    sDiff.changes.snoozed.updated.length +
    sDiff.changes.snoozed.removed.length;
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
  let opSeq = 0;
  let lastActiveElement = null;

  const jsonViewToggles = elements.jsonViewToggles || document.getElementById("json-view-toggles");
  const jsonViewModeLabel = elements.jsonViewModeLabel || document.getElementById("json-view-mode-label");
  const modalCrossWarning = elements.modalCrossWarning || document.getElementById("modal-cross-warning");

  function updateModalContent() {
    // 内容刷新也可能来自外部状态变更，令之前的异步复制回调失效。
    opSeq++;
    elements.copyStatus.textContent = "";
    elements.copyStatus.classList.remove("is-error");
    const oDiff = computeOverridesDiff(overridesState);
    const sDiff = computeSnoozedDiff(overridesState);
    const oCount = ownedState ? calculateOwnedChangesCount(ownedState) : 0;
    const conflicts = detectConflicts(overridesState);

    // 更新 Tab 按钮状态与可访问性属性
    elements.tabModalOverrides.classList.toggle("is-active", currentModalTab === "overrides");
    elements.tabModalOverrides.setAttribute("aria-selected", currentModalTab === "overrides" ? "true" : "false");
    elements.tabModalSnoozed.classList.toggle("is-active", currentModalTab === "snoozed");
    elements.tabModalSnoozed.setAttribute("aria-selected", currentModalTab === "snoozed" ? "true" : "false");
    if (elements.tabModalOwned) {
      elements.tabModalOwned.classList.toggle("is-active", currentModalTab === "owned");
      elements.tabModalOwned.setAttribute("aria-selected", currentModalTab === "owned" ? "true" : "false");
    }

    const tabpanel = document.getElementById("sync-modal-tabpanel");
    if (tabpanel) {
      const activeTabId =
        currentModalTab === "overrides"
          ? "tab-modal-overrides"
          : currentModalTab === "snoozed"
          ? "tab-modal-snoozed"
          : "tab-modal-owned";
      tabpanel.setAttribute("aria-labelledby", activeTabId);
    }

    // 冲突检查拦截
    if (currentModalTab !== "owned" && conflicts.length > 0) {
      if (modalCrossWarning) {
        modalCrossWarning.hidden = false;
        modalCrossWarning.innerHTML =
          "⚠️ <strong>状态冲突警告</strong>：检测到跨集合冲突（" +
          escapeHtml(conflicts.map(c => `${c.skill_id} 同时属于 ${c.collections.join("、")}`).join("；")) +
          "）。为保护仓库配置完整性，已阻止导出与复制，请先清空或重置冲突修改。";
      }
      elements.jsonPreview.textContent =
        "// 检测到跨集合状态冲突，已阻止配置生成与导出：\n" + JSON.stringify(conflicts, null, 2);
      elements.jsonPreview.setAttribute("aria-label", "跨集合冲突警告");
      if (elements.btnCopyJson) elements.btnCopyJson.disabled = true;
      if (elements.btnDownloadJson) elements.btnDownloadJson.disabled = true;
      if (elements.btnGotoGithub) elements.btnGotoGithub.disabled = true;
      elements.copyStatus.classList.add("is-error");
      elements.copyStatus.textContent = "存在状态冲突，导出功能已锁定。";
      return;
    }

    if (elements.btnCopyJson) elements.btnCopyJson.disabled = false;
    if (elements.btnDownloadJson) elements.btnDownloadJson.disabled = false;
    if (elements.btnGotoGithub) elements.btnGotoGithub.disabled = false;

    // 跨集合关联修改提示
    if (modalCrossWarning) {
      if (currentModalTab === "overrides" && sDiff.summary.changed_records > 0) {
        modalCrossWarning.hidden = false;
        modalCrossWarning.textContent =
          "💡 提示：本次修改同时涉及冷冻配置（snoozed.json 尚有 " +
          sDiff.summary.changed_records +
          " 项待同步），请记得切换至对应页签完成同步。";
      } else if (currentModalTab === "snoozed" && oDiff.summary.changed_records > 0) {
        modalCrossWarning.hidden = false;
        modalCrossWarning.textContent =
          "💡 提示：本次修改同时涉及收藏/屏蔽配置（overrides.json 尚有 " +
          oDiff.summary.changed_records +
          " 项待同步），请记得切换至对应页签完成同步。";
      } else {
        modalCrossWarning.hidden = true;
        modalCrossWarning.textContent = "";
      }
    }

    // 隐藏多余的双视图切换按钮组
    if (jsonViewToggles) {
      jsonViewToggles.hidden = true;
      if (jsonViewToggles.style) jsonViewToggles.style.display = "none";
    }
    if (jsonViewModeLabel) {
      jsonViewModeLabel.textContent =
        currentModalTab === "owned"
          ? "已收录变更包（带前置条件，仅供本地合并）"
          : "待同步变更";
    }

    // 内容渲染与按钮文案
    if (currentModalTab === "overrides") {
      elements.modalTitle.textContent = "同步人工干预配置 (overrides.json)";
      elements.modalDesc.innerHTML =
        "以下为本次操作产生的待同步变更记录。可复制或下载变更 JSON，并合入仓库 <code>config/governance/overrides.json</code>。" +
        (oDiff.summary.changed_records === 0 ? " 当前文件没有待同步变更。" : "");
      elements.jsonPreview.textContent = generateIncrementalOverridesJson(overridesState);
      elements.jsonPreview.setAttribute("aria-label", "overrides 待同步变更 JSON 预览");
      elements.btnDownloadJson.textContent = "💾 下载变更 JSON";
      elements.btnCopyJson.textContent = "📋 仅复制变更";
      if (elements.btnGotoGithub) {
        elements.btnGotoGithub.hidden = true;
      }
    } else if (currentModalTab === "snoozed") {
      elements.modalTitle.textContent = "同步暂不关注配置 (snoozed.json)";
      elements.modalDesc.innerHTML =
        "以下为本次操作产生的待同步变更记录。可复制或下载变更 JSON，并合入仓库 <code>config/governance/snoozed.json</code>。" +
        (sDiff.summary.changed_records === 0 ? " 当前文件没有待同步变更。" : "");
      elements.jsonPreview.textContent = generateIncrementalSnoozedJson(overridesState);
      elements.jsonPreview.setAttribute("aria-label", "snoozed 待同步变更 JSON 预览");
      elements.btnDownloadJson.textContent = "💾 下载变更 JSON";
      elements.btnCopyJson.textContent = "📋 仅复制变更";
      if (elements.btnGotoGithub) {
        elements.btnGotoGithub.hidden = true;
      }
    } else {
      elements.modalTitle.textContent = "同步已收录变更包 (owned-patch.json)";
      elements.modalDesc.innerHTML =
        "本站部署于 GitHub Pages 静态环境。请下载下方生成的<strong>带前置条件的已收录变更包</strong>并在本地执行 <code>.\\.venv\\Scripts\\python.exe tools/manage_owned.py --apply-changes owned-patch.json</code> 合并配置并刷新页面数据，随后提交并推送 <code>config/owned-skills.json</code>。Actions 不会自动合并裸变更包。";
      elements.jsonPreview.textContent = ownedState ? generateOwnedPatch(ownedState) : "{}";
      elements.jsonPreview.setAttribute("aria-label", "已收录变更包 JSON 预览");
      elements.btnDownloadJson.textContent = "💾 下载 owned-patch.json";
      elements.btnCopyJson.textContent = "📋 仅复制变更包";
      if (elements.btnGotoGithub) {
        elements.btnGotoGithub.hidden = false;
        elements.btnGotoGithub.textContent = "📋 复制本地合并命令";
        elements.btnGotoGithub.className = "btn-secondary";
      }
    }
  }

  function openSyncModal() {
    opSeq++;
    lastActiveElement = document.activeElement;

    const oDiff = computeOverridesDiff(overridesState);
    const sDiff = computeSnoozedDiff(overridesState);
    const pAndECount = oDiff.summary.changed_records;
    const sCount = sDiff.summary.changed_records;
    const oCount = ownedState ? calculateOwnedChangesCount(ownedState) : 0;

    if (oCount > 0 && pAndECount === 0 && sCount === 0) {
      currentModalTab = "owned";
    } else if (pAndECount > 0) {
      currentModalTab = "overrides";
    } else if (sCount > 0) {
      currentModalTab = "snoozed";
    } else if (oCount > 0) {
      currentModalTab = "owned";
    } else {
      currentModalTab = "overrides";
    }

    elements.copyStatus.textContent = "";
    elements.copyStatus.classList.remove("is-error");
    updateModalContent();
    elements.syncModal.hidden = false;
    elements.btnCloseModal?.focus();
  }

  function closeSyncModal() {
    opSeq++;
    elements.syncModal.hidden = true;
    elements.copyStatus.textContent = "";
    elements.copyStatus.classList.remove("is-error");
    if (lastActiveElement && typeof lastActiveElement.focus === "function") {
      lastActiveElement.focus();
    }
  }

  // 键盘与焦点管理（Tab 焦点限制与 Escape 关闭）
  elements.syncModal.addEventListener("keydown", e => {
    if (e.key === "Escape") {
      e.preventDefault();
      closeSyncModal();
      return;
    }

    if (e.key === "Tab") {
      const focusable = elements.syncModal.querySelectorAll(
        'button:not([disabled]), [tabindex]:not([tabindex="-1"]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled])'
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];

      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  });

  // 页签箭头键导航
  const tabButtons = [elements.tabModalOverrides, elements.tabModalSnoozed, elements.tabModalOwned].filter(Boolean);
  tabButtons.forEach((tabBtn, idx) => {
    tabBtn.addEventListener("keydown", e => {
      let targetIdx = -1;
      if (e.key === "ArrowRight" || e.key === "ArrowDown") {
        targetIdx = (idx + 1) % tabButtons.length;
      } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
        targetIdx = (idx - 1 + tabButtons.length) % tabButtons.length;
      } else if (e.key === "Home") {
        targetIdx = 0;
      } else if (e.key === "End") {
        targetIdx = tabButtons.length - 1;
      }
      if (targetIdx !== -1) {
        e.preventDefault();
        tabButtons[targetIdx].focus();
        tabButtons[targetIdx].click();
      }
    });
  });

  elements.tabModalOverrides.addEventListener("click", () => {
    currentModalTab = "overrides";
    opSeq++;
    elements.copyStatus.textContent = "";
    elements.copyStatus.classList.remove("is-error");
    updateModalContent();
  });

  elements.tabModalSnoozed.addEventListener("click", () => {
    currentModalTab = "snoozed";
    opSeq++;
    elements.copyStatus.textContent = "";
    elements.copyStatus.classList.remove("is-error");
    updateModalContent();
  });

  if (elements.tabModalOwned) {
    elements.tabModalOwned.addEventListener("click", () => {
      currentModalTab = "owned";
      opSeq++;
      elements.copyStatus.textContent = "";
      elements.copyStatus.classList.remove("is-error");
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

  // 仅复制动作
  elements.btnCopyJson.addEventListener("click", () => {
    const thisOp = ++opSeq;
    const jsonStr = elements.jsonPreview.textContent;

    if (navigator && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      navigator.clipboard.writeText(jsonStr).then(() => {
        if (thisOp !== opSeq) return;
        elements.copyStatus.classList.remove("is-error");
        elements.copyStatus.textContent = "✅ 已成功复制 JSON 到剪贴板！";
      }).catch(() => {
        if (thisOp !== opSeq) return;
        elements.copyStatus.classList.add("is-error");
        elements.copyStatus.textContent = "复制失败（剪贴板访问被拒绝），请手动选中文本框内容按 Ctrl+C 复制。";
      });
    } else {
      elements.copyStatus.classList.add("is-error");
      elements.copyStatus.textContent = "当前环境不支持自动复制，请手动选中文本框内容按 Ctrl+C 复制。";
    }
  });

  // 下载文件动作
  elements.btnDownloadJson.addEventListener("click", () => {
    const jsonStr = elements.jsonPreview.textContent;
    let fileName;
    if (currentModalTab === "owned") {
      fileName = "owned-patch.json";
    } else if (currentModalTab === "overrides") {
      fileName = "overrides-changes.json";
    } else {
      fileName = "snoozed-changes.json";
    }

    try {
      const blob = new Blob([jsonStr], { type: "application/json;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = fileName;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      elements.copyStatus.classList.remove("is-error");
      elements.copyStatus.textContent = "✅ 已下载 " + fileName + " 文件。";
    } catch (err) {
      elements.copyStatus.classList.add("is-error");
      elements.copyStatus.textContent = "下载失败：" + (err.message || "未知错误");
    }
  });

  // 主动作按钮交互（仅 owned 页签支持复制本地合并命令）
  if (elements.btnGotoGithub) {
    elements.btnGotoGithub.addEventListener("click", () => {
      if (currentModalTab === "owned") {
        const thisOp = ++opSeq;
        const cmd = ".\\.venv\\Scripts\\python.exe tools/manage_owned.py --apply-changes owned-patch.json";
        if (navigator && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
          navigator.clipboard.writeText(cmd).then(() => {
            if (thisOp !== opSeq) return;
            elements.copyStatus.classList.remove("is-error");
            elements.copyStatus.textContent = "📋 已复制本地合并命令：" + cmd;
          }).catch(() => {
            if (thisOp !== opSeq) return;
            elements.copyStatus.classList.remove("is-error");
            elements.copyStatus.textContent = "本地合并命令：" + cmd;
          });
        } else {
          elements.copyStatus.classList.remove("is-error");
          elements.copyStatus.textContent = "本地合并命令：" + cmd;
        }
      }
    });
  }

  return {
    openSyncModal,
    closeSyncModal,
    updateModalContent,
    getCurrentTab: () => currentModalTab,
    getCurrentViewMode: () => "incremental"
  };
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
