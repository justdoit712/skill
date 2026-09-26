/**
 * 已收录技能管理视图组件（owned-view.js）。
 * 遵循《已收录 Skill 管理：详细实施方案》§6.1, §6.2, §6.4。
 */

import { escapeHtml, text } from "./utils.js";
import {
  validateManagedUrl,
  getPrivateDetails,
  setPrivateDetails,
  removePrivateDetails,
  exportPrivateBackup,
  importPrivateBackup,
  saveOwnedPrivateStorage
} from "./owned-state.js";

const PARTITION_LABELS = {
  recommended: "推荐区",
  candidate: "候选区",
  manual: "收藏区",
  find: "定向查找"
};

/**
 * 转换原分区标识为友好的中文显示。
 */
export function getPartitionLabel(partition) {
  return PARTITION_LABELS[partition] || "候选区";
}

/**
 * 渲染“已收录”主分区卡片列表。
 */
export function renderOwnedList(container, effectiveOwnedList, { q = "" } = {}) {
  container.innerHTML = "";
  const needle = (q || "").trim().toLowerCase();

  const filtered = effectiveOwnedList.filter(item => {
    if (!needle) return true;
    const nameStr = String(item.name || "").toLowerCase();
    const idStr = String(item.skill_id || "").toLowerCase();
    const noteStr = String(item.note || "").toLowerCase();
    return nameStr.includes(needle) || idStr.includes(needle) || noteStr.includes(needle);
  });

  // 顶部工具栏（备份与统计）
  const toolbarLi = document.createElement("li");
  toolbarLi.className = "owned-toolbar-card";
  toolbarLi.innerHTML =
    '<div class="owned-toolbar">' +
      '<div class="owned-toolbar-desc">' +
        '<span>已收录条目（从公开目录及定向查找中隐藏，流水线 0 Token 消耗跳过）：<strong>' + effectiveOwnedList.length + '</strong> 项</span>' +
      '</div>' +
      '<div class="owned-backup-actions">' +
        '<button type="button" class="btn-action" id="btn-export-private-backup" title="导出仅保存在本浏览器的管理链接和私人备注">💾 导出私人备份</button>' +
        '<button type="button" class="btn-action" id="btn-import-private-backup" title="导入私人备份 JSON 文件">📥 导入私人备份</button>' +
      '</div>' +
    '</div>';
  container.appendChild(toolbarLi);

  if (!filtered.length) {
    const emptyLi = document.createElement("li");
    emptyLi.className = "none";
    emptyLi.textContent = needle
      ? "未找到匹配“" + q + "”的已收录条目（支持搜索名称、来源 ID 及私人备注）。"
      : "暂无已收录条目。可在推荐区、候选区、收藏区或定向查找中点击“标为已收录”进行标记。";
    container.appendChild(emptyLi);
    return 0;
  }

  filtered.forEach(item => {
    const sid = item.skill_id;
    const li = document.createElement("li");
    li.className = "card";

    const bits = [
      '<span class="tag tag-owned">✓ 已收录</span>',
      '<span class="tag tag-origin">原：' + escapeHtml(getPartitionLabel(item.original_partition)) + '</span>'
    ];
    if (item.added_at) {
      bits.push('<span class="tag">收录于 ' + escapeHtml(item.added_at) + '</span>');
    }

    let privateHtml = "";
    if (item.managed_url) {
      privateHtml +=
        '<p class="detail private-link">' +
          '<strong>我的管理链接：</strong>' +
          '<a href="' + escapeHtml(item.managed_url) + '" target="_blank" rel="noopener noreferrer">' +
            escapeHtml(item.managed_url) + ' ↗' +
          '</a>' +
        '</p>';
    }
    if (item.note) {
      privateHtml +=
        '<p class="detail private-note">' +
          '<strong>私人备注：</strong>' + escapeHtml(item.note) +
        '</p>';
    }

    const actionsHtml =
      '<button type="button" class="btn-action btn-private" data-action="edit-private" data-id="' + escapeHtml(sid) + '" title="编辑仅保存在当前浏览器的管理链接与备注">📝 私人详情' + (item.managed_url || item.note ? ' (已填)' : '') + '</button>' +
      '<button type="button" class="btn-action btn-unmark" data-action="unmark-owned" data-id="' + escapeHtml(sid) + '" title="取消已收录并按原分区规则恢复">取消已收录</button>';

    li.innerHTML =
      '<div class="card-head">' +
        '<h3><a href="' + escapeHtml(item.source_url || "#") + '" target="_blank" rel="noopener noreferrer">' +
          escapeHtml(item.name || sid) +
        '</a></h3>' +
        '<div class="card-actions">' + actionsHtml + '</div>' +
      '</div>' +
      '<p class="by">' + escapeHtml(sid) + '</p>' +
      '<p class="tags">' + bits.join("") + '</p>' +
      privateHtml;

    container.appendChild(li);
  });

  return filtered.length;
}

/**
 * 显示带撤销（Undo）功能的轻量 Toast 提示。
 */
export function showToast(message, { onUndo = null, duration = 4000 } = {}) {
  let toastContainer = document.getElementById("toast-container");
  if (!toastContainer) {
    toastContainer = document.createElement("div");
    toastContainer.id = "toast-container";
    toastContainer.className = "toast-container";
    document.body.appendChild(toastContainer);
  }

  const toast = document.createElement("div");
  toast.className = "toast-notification";
  toast.innerHTML = '<span>' + escapeHtml(message) + '</span>';

  if (onUndo) {
    const undoBtn = document.createElement("button");
    undoBtn.className = "toast-undo";
    undoBtn.textContent = "撤销";
    undoBtn.addEventListener("click", () => {
      onUndo();
      dismiss();
    });
    toast.appendChild(undoBtn);
  }

  function dismiss() {
    toast.classList.add("toast-fade-out");
    setTimeout(() => {
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }, 200);
  }

  const timer = setTimeout(dismiss, duration);
  toastContainer.appendChild(toast);
}

/**
 * 初始化私人详情编辑弹窗事件。
 */
export function initPrivateDetailsModal(elements, ownedState, onUpdate) {
  let activeSkillId = null;

  function open(skillId) {
    activeSkillId = skillId;
    const detail = getPrivateDetails(ownedState, skillId);
    if (elements.privateSkillIdDisplay) {
      elements.privateSkillIdDisplay.value = skillId;
    }
    if (elements.privateManagedUrl) {
      elements.privateManagedUrl.value = detail.managed_url || "";
    }
    if (elements.privateNote) {
      elements.privateNote.value = detail.note || "";
    }
    if (elements.privateUrlError) {
      elements.privateUrlError.textContent = "";
    }
    if (elements.privateModal) {
      elements.privateModal.hidden = false;
    }
  }

  function close() {
    activeSkillId = null;
    if (elements.privateModal) {
      elements.privateModal.hidden = true;
    }
  }

  if (elements.btnClosePrivateModal) {
    elements.btnClosePrivateModal.addEventListener("click", close);
  }
  if (elements.btnCancelPrivate) {
    elements.btnCancelPrivate.addEventListener("click", close);
  }
  if (elements.privateModal) {
    elements.privateModal.addEventListener("click", e => {
      if (e.target === elements.privateModal) close();
    });
  }

  if (elements.btnSavePrivate) {
    elements.btnSavePrivate.addEventListener("click", () => {
      if (!activeSkillId) return;
      const rawUrl = elements.privateManagedUrl ? elements.privateManagedUrl.value : "";
      const rawNote = elements.privateNote ? elements.privateNote.value : "";

      try {
        setPrivateDetails(ownedState, activeSkillId, {
          managed_url: rawUrl,
          note: rawNote
        });
        saveOwnedPrivateStorage(ownedState);
        close();
        if (onUpdate) onUpdate();
        showToast("✅ 私人详情已保存于当前浏览器。");
      } catch (err) {
        if (elements.privateUrlError) {
          elements.privateUrlError.textContent = err.message || "管理链接格式错误";
        }
      }
    });
  }

  if (elements.btnDeletePrivate) {
    elements.btnDeletePrivate.addEventListener("click", () => {
      if (!activeSkillId) return;
      removePrivateDetails(ownedState, activeSkillId);
      saveOwnedPrivateStorage(ownedState);
      close();
      if (onUpdate) onUpdate();
      showToast("已清除该条目的私人详情。");
    });
  }

  return { open, close };
}

/**
 * 初始化私人备份导入与导出功能。
 */
export function initPrivateBackup(elements, ownedState, onUpdate) {
  // 导出私人备份
  document.addEventListener("click", e => {
    if (e.target && e.target.id === "btn-export-private-backup") {
      const jsonStr = exportPrivateBackup(ownedState);
      const blob = new Blob([jsonStr], { type: "application/json;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "owned-skills.private.json";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      showToast("✅ 已导出私人备份文件：owned-skills.private.json");
    } else if (e.target && e.target.id === "btn-import-private-backup") {
      const fileInput = document.getElementById("private-backup-file-input");
      if (fileInput) {
        fileInput.value = "";
        fileInput.click();
      }
    }
  });

  const fileInput = document.getElementById("private-backup-file-input");
  if (fileInput) {
    fileInput.addEventListener("change", e => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;

      const reader = new FileReader();
      reader.onload = ev => {
        try {
          const content = ev.target.result;
          // 预检查是否有冲突
          const parsed = JSON.parse(content);
          let hasConflict = false;
          if (parsed && parsed.items && typeof parsed.items === "object") {
            Object.entries(parsed.items).forEach(([sid, detail]) => {
              const current = ownedState.privateDetails[sid];
              if (current && (current.managed_url !== (detail.managed_url || "") || current.note !== (detail.note || ""))) {
                hasConflict = true;
              }
            });
          }

          let strategy = "keep_local";
          if (hasConflict) {
            const overwrite = confirm("检测到备份文件中的部分条目与当前浏览器已有私人详情冲突。\n\n点击【确定】使用备份文件覆盖，点击【取消】保留本地已有内容。");
            strategy = overwrite ? "use_imported" : "keep_local";
          }

          const result = importPrivateBackup(ownedState, parsed, { conflictStrategy: strategy });
          saveOwnedPrivateStorage(ownedState);
          if (onUpdate) onUpdate();
          showToast(`✅ 成功导入私人备份：已处理 ${result.importedCount} 条记录${result.conflictCount ? `（${result.conflictCount} 条冲突）` : ""}。`);
        } catch (err) {
          alert("导入私人备份失败：" + (err.message || "文件格式损坏"));
        }
      };
      reader.readAsText(file, "utf-8");
    });
  }
}
