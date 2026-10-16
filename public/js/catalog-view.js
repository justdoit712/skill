/**
 * 技能目录卡片与冷冻列表视图渲染组件。
 * 支持渐进式分页渲染（DOM Pagination），减少首屏超大长列表 DOM 节点开销。
 */

import { escapeHtml, text, day, SKILL_TYPE_LABELS, shanghaiTodayStr } from "./utils.js?v=20260929_sync_2";
import { isPicked } from "./catalog-state.js?v=20260929_keywords_1";

export const DEFAULT_PAGE_SIZE = 24;

/**
 * 判断条目是否满足当前检索、分类与来源筛选条件。
 */
export function matches(entry, queryState = {}) {
  if (queryState.source && entry.source_type !== queryState.source) return false;
  const cat = entry.main_category && entry.main_category.id;
  if (queryState.category && cat !== queryState.category) return false;
  if (!queryState.q) return true;
  const needle = queryState.q.toLowerCase();
  const haystack = [entry.name, entry.summary_zh, entry.author]
    .concat(entry.tags || [])
    .concat(entry.example_requests || [])
    .concat(entry.key_features || []);
  return haystack.some(part => part && String(part).toLowerCase().indexOf(needle) !== -1);
}

/**
 * 渲染变更待复核折叠盒（pending_review）。
 */
export function reviewBlock(entry) {
  const prior = entry.pending_review;
  if (!prior) return "";

  const head = [];
  if (text(prior.evaluated_at)) head.push("评估时间 " + escapeHtml(day(prior.evaluated_at)));
  if (text(prior.rules_version)) head.push("规则版本 " + escapeHtml(prior.rules_version));
  if (text(prior.content_fingerprint)) {
    head.push("对应版本 " + escapeHtml(String(prior.content_fingerprint).slice(0, 18)) + "…");
  }

  const body = [];
  if (text(prior.summary_zh)) body.push("<p>原简述：" + escapeHtml(prior.summary_zh) + "</p>");
  if (text(prior.skill_type) && SKILL_TYPE_LABELS[prior.skill_type]) {
    body.push("<p>原形态：" + escapeHtml(SKILL_TYPE_LABELS[prior.skill_type]) + "</p>");
  }
  if ((prior.example_requests || []).length) {
    body.push("<p>原示例：" + escapeHtml(prior.example_requests.join("；")) + "</p>");
  }
  if ((prior.key_features || []).length) {
    body.push("<p>原亮点：" + escapeHtml(prior.key_features.join("；")) + "</p>");
  }
  if (text(prior.main_category)) body.push("<p>原分类：" + escapeHtml(prior.main_category) + "</p>");
  if (text(prior.limitations)) body.push("<p>原限制：" + escapeHtml(prior.limitations) + "</p>");

  return '<details class="review-box"><summary>原评估版本' +
    (head.length ? "（" + head.join(" · ") + "）" : "") +
    "</summary>" + (body.join("") || "<p>原评估未保留可展示的简述。</p>") + "</details>";
}

export function qualityBlock(entry) {
  const quality = entry.quality_summary;
  if (!quality) return "";
  const names = { scope_match: "范围匹配", purpose_clarity: "用途与价值", instruction_completeness: "说明与做法", evidence_traceability: "内容依据", dependency_transparency: "依赖与适用条件", risk_review: "明显风险", practical_value: "实际价值", actionability: "可执行性", verification: "结果验证" };
  const values = { pass: "通过", fail: "未通过", unknown: "待核实" };
  const states = { passed: "两轮评估通过", disagreed: "两轮评估有分歧", not_required: "初评未达到推荐门槛", budget_stopped: "待完成复核", usage_unknown: "待完成复核" };
  const rows = [];
  for (const [key, label] of Object.entries(names)) {
    const check = (quality.checks || {})[key];
    if (check) rows.push("<p>" + label + "：" + escapeHtml(check.informational ? "提示" : (values[check.value] || "待核实")) + " — " + escapeHtml(check.evidence || "") + "</p>");
    const review = (quality.review_checks || {})[key];
    if (review && review.value !== "pass") rows.push("<p>复核意见（" + label + "）：" + escapeHtml(review.evidence || "") + "</p>");
  }
  if (quality.verification_note) rows.push("<p>验证说明：" + escapeHtml(quality.verification_note) + "</p>");
  if (quality.review_note) rows.push("<p>" + escapeHtml(quality.review_note) + "</p>");
  for (const reason of quality.blocking_reasons || []) rows.push("<p>" + escapeHtml(reason) + "</p>");
  const status = quality.review_status === "single_pass" ? "" : " · " + (states[quality.review_status] || "待核实");
  return '<details class="review-box"><summary>筛选依据' + escapeHtml(status) + '</summary>' + rows.join("") + '<p>基于所提供材料评估，未经功能实测。</p></details>';
}

export function getCategoryIcon(catId, catName) {
  const combined = (String(catId || '') + ' ' + String(catName || '')).toLowerCase();
  if (combined.includes('design') || combined.includes('产品') || combined.includes('设计') || combined.includes('style') || combined.includes('ui')) return '🎨';
  if (combined.includes('data') || combined.includes('数据') || combined.includes('分析') || combined.includes('finance')) return '📊';
  if (combined.includes('dev') || combined.includes('研发') || combined.includes('工程') || combined.includes('code') || combined.includes('tool')) return '⚡';
  if (combined.includes('doc') || combined.includes('文档') || combined.includes('写作') || combined.includes('coauthor')) return '📝';
  if (combined.includes('workflow') || combined.includes('工作流') || combined.includes('自动化') || combined.includes('auto')) return '⚙️';
  if (combined.includes('media') || combined.includes('音视频') || combined.includes('ppt') || combined.includes('presentation')) return '🎬';
  return '📦';
}

/**
 * 渲染侧边抽屉完整详情内容。
 */
export function renderDrawerContent(entry, overridesState, currentTab = "recommended") {
  if (!entry) return "";
  const sid = entry.skill_id;
  const picked = isPicked(overridesState, sid);

  let examplesHtml = "";
  if ((entry.example_requests || []).length) {
    const reqItems = entry.example_requests.map(req => {
      return '<div class="drawer-example-item"><code>' + escapeHtml(req) + '</code></div>';
    }).join("");
    examplesHtml = '<div class="drawer-section"><h4>💡 示例请求</h4><div class="drawer-examples-list">' + reqItems + '</div></div>';
  }

  let featuresHtml = "";
  if ((entry.key_features || []).length) {
    const featItems = entry.key_features.map(feat => {
      return '<li class="drawer-feature-item">' + escapeHtml(feat) + '</li>';
    }).join("");
    featuresHtml = '<div class="drawer-section"><h4>✨ 亮点特性</h4><ul class="drawer-features-list">' + featItems + '</ul></div>';
  }

  const metaRows = [];
  if (text(entry.platform_declared)) metaRows.push('<tr><th>运行平台</th><td>' + escapeHtml(entry.platform_declared) + '</td></tr>');
  if ((entry.dependencies_declared || []).length) {
    metaRows.push('<tr><th>依赖声明</th><td>' + escapeHtml(entry.dependencies_declared.join("、")) + '</td></tr>');
  }
  if (text(entry.limitations)) metaRows.push('<tr><th>使用限制</th><td>' + escapeHtml(entry.limitations) + '</td></tr>');
  if (text(entry.license)) metaRows.push('<tr><th>开源许可</th><td>' + escapeHtml(entry.license) + '</td></tr>');
  if (text(entry.first_seen)) metaRows.push('<tr><th>首次发现</th><td>' + escapeHtml(entry.first_seen.slice(0, 10)) + '</td></tr>');
  if (text(entry.last_checked)) metaRows.push('<tr><th>最近检查</th><td>' + escapeHtml(entry.last_checked.slice(0, 10)) + '</td></tr>');
  if (text(entry.content_changed_at)) metaRows.push('<tr><th>内容变更</th><td>' + escapeHtml(entry.content_changed_at.slice(0, 10)) + '</td></tr>');

  const metaTable = metaRows.length ? '<div class="drawer-section"><h4>📋 元数据与环境</h4><table class="drawer-meta-table"><tbody>' + metaRows.join("") + '</tbody></table></div>' : '';

  return '<div class="drawer-summary-box"><p class="drawer-summary-text">' +
    escapeHtml(text(entry.summary_zh) || "（暂无中文简述）") + '</p></div>' +
    featuresHtml +
    examplesHtml +
    '<div class="drawer-section"><h4>🛡️ 评估依据与复核</h4>' +
    reviewBlock(entry) +
    qualityBlock(entry) +
    '</div>' +
    metaTable;
}

/**
 * 创建单张技能卡片 DOM 节点。
 */
export function createSkillCard(entry, overridesState, currentTab = "recommended") {
  const sid = entry.skill_id;
  const picked = isPicked(overridesState, sid);
  const li = document.createElement("li");
  li.className = "card";
  li.setAttribute("data-sid", sid);

  const cat = entry.main_category ? entry.main_category.name : "未分类";
  const icon = getCategoryIcon(entry.main_category?.id, cat);

  const bits = ['<span class="tag tag-cat">' + escapeHtml(cat) + "</span>"];
  if (entry.skill_type && SKILL_TYPE_LABELS[entry.skill_type]) {
    bits.push('<span class="tag tag-type tag-type-' + escapeHtml(entry.skill_type) + '">' +
      escapeHtml(SKILL_TYPE_LABELS[entry.skill_type]) + '</span>');
  }
  if (picked) {
    bits.push('<span class="tag tag-manual">收藏</span>');
  }
  if (entry.needs_review) {
    bits.push('<span class="tag tag-review">待复核</span>');
  }
  if (entry.status === "candidate" && !entry.needs_review && !picked) {
    bits.push('<span class="tag tag-cand">候选</span>');
  }

  let featuresHtml = "";
  if ((entry.key_features || []).length) {
    const feat = entry.key_features[0];
    featuresHtml = '<div class="card-features"><span class="feature-item" title="' + escapeHtml(entry.key_features.join("；")) + '">✨ ' + escapeHtml(feat) + '</span></div>';
  }

  const favBtnClass = picked ? "btn-action btn-fav is-active" : "btn-action btn-fav";
  const favBtnText = picked ? "★ 已收藏" : "★ 收藏";
  const ownedBtn = '<button type="button" class="btn-action btn-owned" data-action="owned" data-id="' + escapeHtml(sid) + '" title="标记为已收录（需二次确认，从目录与查找中隐藏，0 Token 跳过）">标为已收录</button>';

  let actionsHtml = "";
  if (currentTab === "manual") {
    actionsHtml =
      ownedBtn +
      '<button type="button" class="' + favBtnClass + '" data-action="fav" data-id="' + escapeHtml(sid) + '">' + favBtnText + '</button>' +
      '<button type="button" class="btn-action btn-block" data-action="block" data-id="' + escapeHtml(sid) + '" title="屏蔽并移入黑名单">🚫 屏蔽</button>';
  } else {
    actionsHtml =
      '<button type="button" class="' + favBtnClass + '" data-action="fav" data-id="' + escapeHtml(sid) + '">' + favBtnText + '</button>' +
      '<button type="button" class="btn-action btn-snooze" data-action="snooze" data-id="' + escapeHtml(sid) + '" title="暂不关注（冷冻150天，到期自动恢复）">⏳ 暂不看</button>' +
      '<button type="button" class="btn-action btn-block" data-action="block" data-id="' + escapeHtml(sid) + '" title="屏蔽并移入黑名单">🚫 屏蔽</button>';
  }

  const authorHtml = entry.author
    ? '<p class="by"><span class="author-label">by</span> <span class="author-name">@' + escapeHtml(entry.author) + '</span></p>'
    : '';

  li.innerHTML =
    '<div class="card-head">' +
      '<div class="card-brand">' +
        '<span class="card-cat-icon" aria-hidden="true">' + icon + '</span>' +
        '<div class="card-title-group">' +
          '<h3 class="card-title"><a href="' + escapeHtml(entry.url || "#") + '" target="_blank" rel="noopener noreferrer" data-prevent-drawer="true">' +
          escapeHtml(entry.name) + "</a></h3>" +
          authorHtml +
        '</div>' +
      '</div>' +
      '<div class="card-actions">' + actionsHtml + '</div>' +
    '</div>' +
    '<p class="sum" title="' + escapeHtml(text(entry.summary_zh) || "") + '">' + escapeHtml(text(entry.summary_zh) || "（暂无中文简述）") + "</p>" +
    featuresHtml +
    '<div class="card-foot">' +
      '<div class="tags">' + bits.join("") + '</div>' +
      '<button type="button" class="btn-card-detail" data-action="open-detail" data-id="' + escapeHtml(sid) + '">详情 ↗</button>' +
    '</div>' +
    '<div class="card-collapsible-details" hidden>' +
      reviewBlock(entry) +
      qualityBlock(entry) +
    '</div>';

  return li;
}

/**
 * 渲染技能目录列表（支持渐进式分页分批渲染，降低超长列表 DOM 节点开销）。
 *
 * @param {HTMLElement} container 挂载容器
 * @param {Array} entries 当前分区条目数组
 * @param {Object} overridesState 收藏/排除/冷冻状态
 * @param {string} currentTab 当前选中的 Tab ('recommended' | 'candidate' | 'manual')
 * @param {Object} queryState 搜索关键词与分类过滤状态
 * @param {Object} options 分页选项：{ pageSize: 24, all: false, onBatchRendered: (rendered, total) => void }
 * @returns {number} 返回当前筛选下匹配的条目总数
 */
export function renderCatalogList(
  container,
  entries,
  overridesState,
  currentTab = "recommended",
  queryState = {},
  options = {}
) {
  const visible = entries.filter(e => matches(e, queryState));
  container.innerHTML = "";

  if (!visible.length) {
    container.innerHTML = '<li class="none">当前筛选下没有条目。</li>';
    return 0;
  }

  const pageSize = options.pageSize || DEFAULT_PAGE_SIZE;
  const initialLimit = options.all ? visible.length : Math.min(pageSize, visible.length);

  let renderedCount = 0;
  let loadMoreLi = null;

  function removeLoadMore() {
    if (!loadMoreLi) return;
    if (typeof loadMoreLi.remove === "function") {
      loadMoreLi.remove();
    } else if (container.children && Array.isArray(container.children)) {
      const idx = container.children.indexOf(loadMoreLi);
      if (idx !== -1) container.children.splice(idx, 1);
    }
    loadMoreLi = null;
  }

  function renderBatch(start, count) {
    removeLoadMore();
    const end = Math.min(start + count, visible.length);
    for (let i = start; i < end; i++) {
      const card = createSkillCard(visible[i], overridesState, currentTab);
      container.appendChild(card);
    }
    renderedCount = end;

    if (renderedCount < visible.length) {
      const remaining = visible.length - renderedCount;
      loadMoreLi = document.createElement("li");
      loadMoreLi.className = "catalog-pagination";
      const btnText = `加载更多条目（还剩 ${remaining} 条）`;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn-load-more";
      btn.textContent = btnText;
      btn.innerHTML = escapeHtml(btnText);
      btn.addEventListener("click", () => {
        renderBatch(renderedCount, pageSize);
      });
      loadMoreLi.appendChild(btn);
      container.appendChild(loadMoreLi);
    }

    if (typeof options.onBatchRendered === "function") {
      options.onBatchRendered(renderedCount, visible.length);
    }
  }

  renderBatch(0, initialLimit);
  return visible.length;
}

/**
 * 渲染冷冻弹窗中的条目列表。
 */
export function renderSnoozedList(container, effectiveSnoozedList, allEntries, today = null) {
  container.innerHTML = "";
  if (!effectiveSnoozedList.length) {
    container.innerHTML = '<p style="text-align:center;color:#94a3b8;margin:16px 0;">当前没有处于冷冻期的条目。</p>';
    return;
  }
  const curToday = today || shanghaiTodayStr();
  const pToday = curToday.split("-").map(Number);
  const msPerDay = 24 * 60 * 60 * 1000;
  const dToday = Date.UTC(pToday[0], pToday[1] - 1, pToday[2]);

  effectiveSnoozedList.forEach(item => {
    const entry = allEntries[item.skill_id];
    const name = entry ? entry.name : item.skill_id;
    const url = entry ? (entry.url || "#") : "#";

    const pExp = item.expires_at.split("-").map(Number);
    const dExp = Date.UTC(pExp[0], pExp[1] - 1, pExp[2]);
    const remainDays = Math.max(0, Math.round((dExp - dToday) / msPerDay));

    const row = document.createElement("div");
    row.className = "snoozed-row";
    row.innerHTML =
      '<div class="snoozed-row-info">' +
        '<div class="snoozed-row-id">' +
          '<a href="' + escapeHtml(url) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(name) + '</a>' +
        '</div>' +
        '<div class="snoozed-row-dates">' +
          '冷冻于 ' + escapeHtml(item.snoozed_at) + ' · 到期恢复 ' + escapeHtml(item.expires_at) +
          '（还剩 ' + remainDays + ' 天）' +
        '</div>' +
        '<div class="snoozed-row-reason">' +
          escapeHtml(item.reason || "用户暂不关注") +
        '</div>' +
      '</div>' +
      '<button type="button" class="btn-action" data-action="unsnooze" data-id="' + escapeHtml(item.skill_id) + '">恢复显示</button>';
    container.appendChild(row);
  });
}
