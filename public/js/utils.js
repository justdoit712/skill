/**
 * 通用无副作用工具函数与常量字典。
 */

export const SKILL_TYPE_LABELS = {
  tool_script: "工具脚本",
  guideline: "规范指南",
  template: "文档模板",
  reference: "查阅手册"
};

/**
 * 获取当前 Asia/Shanghai 时区 YYYY-MM-DD 日期字符串。
 */
export function shanghaiTodayStr() {
  const d = new Date();
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  });
  return formatter.format(d);
}

/**
 * 计算冷冻到期日：snoozed_at + days 天。
 */
export function computeExpiresAt(snoozedAtStr, days = 150) {
  const parts = String(snoozedAtStr).trim().split("-").map(Number);
  const d = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2]));
  d.setUTCDate(d.getUTCDate() + days);
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, "0");
  const day = String(d.getUTCDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/**
 * 判断单个冷冻记录是否处于活跃冷冻期内（snoozed_at <= today < expires_at）。
 */
export function isSnoozeActive(item, today) {
  const currentToday = today || shanghaiTodayStr();
  if (!item || !item.snoozed_at || !item.expires_at) return false;
  return item.snoozed_at <= currentToday && currentToday < item.expires_at;
}

/**
 * 格式化非空文本字符串，空值返回 null。
 */
export function text(value) {
  return value === null || value === undefined || value === "" ? null : String(value);
}

/**
 * HTML 转义，防止 XSS 注入。
 */
export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

/**
 * 截取前 10 位日期（YYYY-MM-DD）。
 */
export function day(value) {
  return text(value) ? String(value).slice(0, 10) : null;
}
