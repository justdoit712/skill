/**
 * 已收录技能提示视图组件（owned-view.js）。
 * 遵循 GitHub Pages 纯静态展示定位，仅保留轻量操作反馈（Toast & Undo）。
 */

import { escapeHtml } from "./utils.js";

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
