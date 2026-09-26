/**
 * 技能目录卡片与冷冻列表视图渲染组件。
 */

import { escapeHtml, text, day, SKILL_TYPE_LABELS, shanghaiTodayStr } from "./utils.js";
import { isPicked } from "./catalog-state.js";

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
  const names = { practical_value: "实际价值", actionability: "可执行性", verification: "结果验证" };
  const values = { pass: "通过", fail: "未通过", unknown: "待核实" };
  const states = { passed: "两轮评估通过", disagreed: "两轮评估有分歧", not_required: "初评未达到推荐门槛", budget_stopped: "待完成复核", usage_unknown: "待完成复核" };
  const rows = [];
  for (const [key, label] of Object.entries(names)) {
    const check = (quality.checks || {})[key];
    if (check) rows.push("<p>" + label + "：" + escapeHtml(values[check.value] || "待核实") + " — " + escapeHtml(check.evidence || "") + "</p>");
    const review = (quality.review_checks || {})[key];
    if (review && review.value !== "pass") rows.push("<p>复核意见（" + label + "）：" + escapeHtml(review.evidence || "") + "</p>");
  }
  if (quality.review_note) rows.push("<p>" + escapeHtml(quality.review_note) + "</p>");
  for (const reason of quality.blocking_reasons || []) rows.push("<p>" + escapeHtml(reason) + "</p>");
  return '<details class="review-box"><summary>筛选依据 · ' + escapeHtml(states[quality.review_status] || "待核实") + '</summary>' + rows.join("") + '<p>基于所提供材料评估，未经功能实测。</p></details>';
}

/**
 * 渲染技能目录列表。
 */
export function renderCatalogList(container, entries, overridesState, currentTab = "recommended", queryState = {}) {
  const visible = entries.filter(e => matches(e, queryState));
  container.innerHTML = "";

  if (!visible.length) {
    container.innerHTML = '<li class="none">当前筛选下没有条目。</li>';
    return 0;
  }

  visible.forEach(entry => {
    const sid = entry.skill_id;
    const picked = isPicked(overridesState, sid);
    const li = document.createElement("li");
    li.className = "card";

    const cat = entry.main_category ? entry.main_category.name : "未分类";
    const bits = ['<span class="tag tag-cat">' + escapeHtml(cat) + "</span>"];
    if (entry.skill_type && SKILL_TYPE_LABELS[entry.skill_type]) {
      bits.push('<span class="tag tag-type tag-type-' + escapeHtml(entry.skill_type) + '">' +
        escapeHtml(SKILL_TYPE_LABELS[entry.skill_type]) + '</span>');
    }
    if (picked) {
      bits.push('<span class="tag tag-manual">收藏</span>');
    }
    if (entry.needs_review) {
      bits.push('<span class="tag tag-review">内容已变化，待复核' +
        (entry.status === "recommended" ? "（推荐状态待复核）" : "（已降级）") + "</span>");
    }
    if (entry.status === "candidate" && !entry.needs_review && !picked) {
      bits.push('<span class="tag tag-cand">候选</span>');
    }
    if (entry.source_type) {
      bits.push('<span class="tag">' + escapeHtml(entry.source_type) + "</span>");
    }
    (entry.tags || []).forEach(t => {
      bits.push('<span class="tag">' + escapeHtml(t) + "</span>");
    });

    const meta = [];
    if (text(entry.platform_declared)) meta.push("平台：" + escapeHtml(entry.platform_declared));
    if ((entry.dependencies_declared || []).length) {
      meta.push("依赖：" + escapeHtml(entry.dependencies_declared.join("、")));
    }
    if (text(entry.limitations)) meta.push("限制：" + escapeHtml(entry.limitations));
    if (text(entry.license)) meta.push("许可：" + escapeHtml(entry.license));

    let examplesHtml = "";
    if ((entry.example_requests || []).length) {
      const reqItems = entry.example_requests.map(req => {
        return '<span class="example-item">' + escapeHtml(req) + '</span>';
      }).join("");
      examplesHtml = '<div class="card-examples"><span class="example-label">示例请求：</span>' + reqItems + '</div>';
    }

    let featuresHtml = "";
    if ((entry.key_features || []).length) {
      const featItems = entry.key_features.map(feat => {
        return '<span class="feature-item">' + escapeHtml(feat) + '</span>';
      }).join("");
      featuresHtml = '<div class="card-features"><span class="feature-label">亮点：</span>' + featItems + '</div>';
    }

    let manualHtml = "";
    if (picked) {
      const note = overridesState.stagedPicks[sid] || entry.manual_note || overridesState.baselinePicks[sid] || {};
      manualHtml += '<p class="detail manual">收藏理由：' + escapeHtml(note.reason || "人工收藏") +
        (note.added_at ? '（' + escapeHtml(note.added_at) + '）' : '') + '</p>';
      if (entry.content_changed_at) {
        manualHtml += '<p class="detail changed">上游内容已变化（' + escapeHtml(entry.content_changed_at.slice(0, 10)) + '），尚未复核；本条为人工收藏，程序不会自动重评。</p>';
      }
      const autoExcluded = entry.status === "excluded" || (note.auto_status === "excluded");
      if (autoExcluded) {
        const reasons = (entry.reason_codes || []).join("、");
        manualHtml += '<p class="detail warning-box">程序判定为排除项' + (reasons ? '（' + escapeHtml(reasons) + '）' : '') + '，本条为人工收藏，请自行确认风险。</p>';
      } else if (entry.status === "processing_failure") {
        manualHtml += '<p class="detail warning-box">评估未完成。</p>';
      }
      if (entry.upstream_status && entry.upstream_status !== "ok") {
        manualHtml += '<p class="detail warning-box">上游不可访问' + (entry.last_checked ? '（' + escapeHtml(entry.last_checked.slice(0, 10)) + '）' : '') + '。</p>';
      }
    }

    const dates = [];
    if (text(entry.first_seen)) dates.push("首次发现 " + escapeHtml(entry.first_seen.slice(0, 10)));
    if (text(entry.last_checked)) dates.push("最近检查 " + escapeHtml(entry.last_checked.slice(0, 10)));
    if (text(entry.content_changed_at)) dates.push("内容变更 " + escapeHtml(entry.content_changed_at.slice(0, 10)));
    if (text(entry.upstream_status) && entry.upstream_status !== "ok") {
      dates.push("上游状态 " + escapeHtml(entry.upstream_status));
    }

    const favBtnClass = picked ? "btn-action btn-fav is-active" : "btn-action btn-fav";
    const favBtnText = picked ? "★ 已收藏" : "★ 收藏";
    const ownedBtn = '<button type="button" class="btn-action btn-owned" data-action="owned" data-id="' + escapeHtml(sid) + '" title="标记为已收录（需二次确认，从目录与查找中隐藏，0 Token 跳过）">标为已收录</button>';

    // 仅收藏区展示“标为已收录”（点击需二次确认）；推荐与候选区展示收藏、暂不看、屏蔽
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

    li.innerHTML =
      '<div class="card-head">' +
        '<h3><a href="' + escapeHtml(entry.url || "#") + '" target="_blank" rel="noopener noreferrer">' +
        escapeHtml(entry.name) + "</a></h3>" +
        '<div class="card-actions">' + actionsHtml + '</div>' +
      '</div>' +
      '<p class="by">' + escapeHtml(entry.author || "") + "</p>" +
      '<p class="sum">' + escapeHtml(text(entry.summary_zh) || "（暂无中文简述）") + "</p>" +
      examplesHtml +
      featuresHtml +
      '<p class="tags">' + bits.join("") + "</p>" +
      (meta.length ? '<p class="detail">' + meta.join(" · ") + "</p>" : "") +
      manualHtml +
      (entry.needs_review ? '<p class="detail review">待复核：' +
        escapeHtml(entry.review_note || "上游内容已变化，等待复核。") + "</p>" : "") +
      reviewBlock(entry) +
      qualityBlock(entry) +
      (dates.length ? '<p class="dates">' + dates.join(" · ") + "</p>" : "");
    container.appendChild(li);
  });

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
      '</div>' +
      '<button type="button" class="btn-action" data-action="unsnooze" data-id="' + escapeHtml(item.skill_id) + '">恢复显示</button>';
    container.appendChild(row);
  });
}
