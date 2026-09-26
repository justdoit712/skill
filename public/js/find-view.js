/**
 * 定向技能查找报告（find-report）视图渲染组件。
 * 遵循《已收录 Skill 管理：详细实施方案》§6.3。
 */

import { escapeHtml } from "./utils.js";
import { isOwned } from "./owned-state.js";

/**
 * 渲染定向查找报告。
 * 纯投影：不修改原 report 对象。已收录条目自动隐藏并显示统计。
 */
export function renderFindView(container, report, ownedState = null) {
  container.innerHTML = "";
  if (report?.schema_version && report.schema_version !== "1.0.0") {
    container.innerHTML = '<li class="find-overview-card"><h3>报告版本不兼容</h3><p>请升级页面后重新查看该报告。</p></li>';
    return;
  }
  if (!report || (!report.shortlist && !report.alternatives)) {
    container.innerHTML =
      '<li class="find-overview-card find-empty-box">' +
        '<div class="find-empty-icon">🔍</div>' +
        '<h3>暂无定向查找结果</h3>' +
        '<p class="hint">您可以在本地终端或 PyCharm 中运行 <code>python tools/find_skill.py</code> 发起特定技能的定向检索，生成结果将在此自动刷新展示。</p>' +
      '</li>';
    return;
  }

  const topic = report.topic || "未命名需求";
  const updated = report.updated_at ? report.updated_at.slice(0, 19).replace("T", " ") : "未知";

  const rawShortlist = report.shortlist || [];
  const rawAlternatives = report.alternatives || [];
  const shortlist = rawShortlist.filter(item => !ownedState || !isOwned(ownedState, (item.candidate || item).skill_id));
  const alternatives = rawAlternatives.filter(item => !ownedState || !isOwned(ownedState, (item.candidate || item).skill_id));
  const hiddenOwnedCount = (rawShortlist.length + rawAlternatives.length) - (shortlist.length + alternatives.length);

  const tokens = Number.isFinite(report.usage?.total_tokens) ? report.usage.total_tokens.toLocaleString() : "未知";
  const evCount = Number.isFinite(report.evaluated_count) ? report.evaluated_count : "未知";
  const attempts = Number.isFinite(report.evaluation_attempts) ? report.evaluation_attempts : "未知";
  const stopReason = (report.stop_reason || report.status || "").toLowerCase();

  // 状态提示条（支持 usage_unknown, token_limit, evaluation_limit, all_candidates_owned, interrupted 等）
  let bannerHtml = "";
  if (report.status === "running") {
    bannerHtml = '<div class="find-status-banner info">查找尚未完成；此处显示上次保存的进度。</div>';
  } else if (stopReason === "all_candidates_owned") {
    bannerHtml = '<div class="find-status-banner info">ℹ️ 本次发现的候选已全部收录。</div>';
  } else if (stopReason === "usage_unknown") {
    bannerHtml = '<div class="find-status-banner warning">⚠️ 模型调用缺失用量统计 (usage_unknown)，触发安全停机保护；已安全保存中断前的全部局部结果。</div>';
  } else if (stopReason === "token_limit") {
    bannerHtml = '<div class="find-status-banner warning">⚠️ 模型调用消耗已达到本次 Token 预算上限 (token_limit)，查找停止；已保存当前已完成结果。</div>';
  } else if (stopReason === "evaluation_limit") {
    bannerHtml = '<div class="find-status-banner info">ℹ️ 评估数量已达到本次设置上限 (evaluation_limit)，查找停止。</div>';
  } else if (stopReason === "round_limit") {
    bannerHtml = '<div class="find-status-banner info">已达到检索轮数上限，尚未集齐目标数量；当前结果已保存，可增加轮数后续跑。</div>';
  } else if (stopReason === "target_reached") {
    bannerHtml = '<div class="find-status-banner info">已集齐目标短名单，查找提前完成。</div>';
  } else if (stopReason === "interrupted") {
    bannerHtml = '<div class="find-status-banner warning">⏸️ 用户主动中断查找过程 (interrupted)；已安全保留中断前已评估的全部结果与 Token 用量。</div>';
  } else if (stopReason === "model_failures") {
    bannerHtml = '<div class="find-status-banner danger">⚠️ 连续模型调用异常次数超标 (model_failures)，触发熔断停机保护。</div>';
  } else if (report.status === "error" || stopReason === "search_failed" || stopReason === "plan_failed") {
    bannerHtml = '<div class="find-status-banner danger">❌ 查找过程中发生异常中止：' + escapeHtml(report.stop_reason || report.status) + '</div>';
  }
  if (report.coverage_incomplete) {
    bannerHtml += '<div class="find-status-banner warning">本次检索覆盖不完整，部分来源读取失败或超出读取范围。</div>';
  }
  if (report.usage?.unknown_usage_requests > 0) {
    bannerHtml += '<div class="find-status-banner warning">存在用量未知的请求，显示的 Token 仅为已知用量。</div>';
  }

  // 已收录隐藏提示
  if (hiddenOwnedCount > 0) {
    if (rawShortlist.length + rawAlternatives.length > 0 && shortlist.length + alternatives.length === 0) {
      bannerHtml += '<div class="find-status-banner info">ℹ️ 本报告中的条目已全部收录，已自动隐藏。</div>';
    } else {
      bannerHtml += '<div class="find-status-banner info">ℹ️ 原报告推荐 ' + rawShortlist.length + ' 项、备选 ' + rawAlternatives.length + ' 项；当前已收录隐藏 ' + hiddenOwnedCount + ' 项。</div>';
    }
  }

  // 1. 概况卡片
  const overviewLi = document.createElement("li");
  overviewLi.className = "find-overview-card";

  let criteriaHtml = "";
  if (report.plan && Array.isArray(report.plan.criteria)) {
    const cItems = report.plan.criteria.map(c => {
      const kindBadge = c.kind === "required" ? '<strong style="color:#dc2626;">[必选]</strong>' : '<span style="color:#0284c7;">[加分]</span>';
      return "<li>" + kindBadge + " " + escapeHtml(c.description) + "</li>";
    }).join("");
    criteriaHtml =
      '<details class="find-criteria-box" open>' +
        '<summary>📋 本次自动拆解的质量评判准则（' + report.plan.criteria.length + ' 项）</summary>' +
        '<ul class="find-criteria-list">' + cItems + '</ul>' +
      '</details>';
  }

  overviewLi.innerHTML =
    '<div class="find-overview-header">' +
      '<h2 class="find-topic-title">定向查找目标：<span>' + escapeHtml(topic) + '</span></h2>' +
    '</div>' +
    '<div class="find-stats-row">' +
      '<span>🏆 优先推荐：<strong>' + shortlist.length + '</strong> 项</span>' +
      '<span>📋 相关备选：<strong>' + alternatives.length + '</strong> 项</span>' +
      '<span>🔍 评估条目：<strong>' + evCount + '</strong>（尝试 <strong>' + attempts + '</strong> 次）</span>' +
      '<span>⚡ 消耗 Token：<strong>' + tokens + '</strong></span>' +
      '<span>🕒 时间：<strong>' + escapeHtml(updated) + '</strong></span>' +
    '</div>' +
    bannerHtml +
    criteriaHtml;
  container.appendChild(overviewLi);

  // 2. 优先推荐短名单
  if (shortlist.length > 0) {
    const div1 = document.createElement("li");
    div1.className = "find-section-divider";
    div1.innerHTML = '<span>🏆 优先推荐短名单（' + shortlist.length + '）</span>';
    container.appendChild(div1);

    shortlist.forEach(item => {
      const cand = item.candidate || item;
      const ev = item.evaluation || item;
      const li = document.createElement("li");
      li.className = "card";

      const matchLevel = ev.match || item.match_level || "strong";
      const docLevel = ev.documentation || "clear";
      const bits = [
        '<span class="tag tag-find-strong">' + (matchLevel === "strong" ? "强匹配" : escapeHtml(matchLevel)) + '</span>',
        '<span class="tag tag-find-doc">' + (docLevel === "clear" ? "说明完整" : "说明部分") + '</span>'
      ];
      const authorName = cand.author || item.author;
      if (authorName) bits.push('<span class="tag">' + escapeHtml(authorName) + '</span>');

      let evidenceHtml = "";
      const supportedResults = (ev.criteria_results || []).filter(cr => cr.status === "supported");
      if (supportedResults.length > 0) {
        const evItems = supportedResults.map(cr => {
          const quotes = (cr.evidence || []).map(e => {
            const loc = e.source_path ? (e.source_path + (e.start_line ? "#L" + e.start_line + "-L" + e.end_line : "")) : "";
            const qText = e.quote ? '<div class="find-evidence-quote">“' + escapeHtml(e.quote) + '”</div>' : "";
            return '<div class="find-evidence-loc">📍 ' + escapeHtml(loc) + '</div>' + qText;
          }).join("");
          return '<div class="find-evidence-item"><strong>✔ ' + escapeHtml(cr.criterion_id) + '</strong>：' + escapeHtml(cr.explanation || "支持该需求") + quotes + '</div>';
        }).join("");
        evidenceHtml =
          '<details class="find-evidence-box" open>' +
            '<summary>已核验代码证据（' + supportedResults.length + ' 项通过原文比对）</summary>' +
            evItems +
          '</details>';
      }

      const whyHtml = ev.why_consider ? '<div class="find-why-box">💡 <strong>推荐理由：</strong>' + escapeHtml(ev.why_consider) + '</div>' : '';
      const usageHtml = ev.usage_zh ? '<p class="detail"><strong>使用方式：</strong>' + escapeHtml(ev.usage_zh) + '</p>' : '';
      const depHtml = (ev.dependencies || []).length ? '<p class="detail"><strong>依赖：</strong>' + escapeHtml(ev.dependencies.join("、")) + '</p>' : '';
      const limHtml = (ev.limitations || []).length ? '<p class="detail" style="color:#b45309;"><strong>限制与注意：</strong>' + escapeHtml(ev.limitations.join("；")) + '</p>' : '';
      const skillName = cand.name || cand.skill_id || item.name || item.skill_id || "未命名技能";
      const skillUrl = safeLink(cand.url || cand.repo_url || item.url || item.repo_url);
      const summaryText = ev.summary_zh || item.summary || "无简述";
      const candId = cand.skill_id || item.skill_id;

      li.innerHTML =
        '<div class="card-head">' +
          '<h3><a href="' + escapeHtml(skillUrl) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(skillName) + '</a></h3>' +
        '</div>' +
        '<div class="card-tags" style="margin: 0 0 8px;">' + bits.join("") + '</div>' +
        '<p class="summary">' + escapeHtml(summaryText) + '</p>' +
        whyHtml +
        evidenceHtml +
        usageHtml +
        depHtml +
        limHtml;
      container.appendChild(li);
    });
  }

  // 3. 相关备选及差距
  if (alternatives.length > 0) {
    const div2 = document.createElement("li");
    div2.className = "find-section-divider";
    div2.innerHTML = '<span>📋 相关备选及差距（' + alternatives.length + '）</span>';
    container.appendChild(div2);

    alternatives.forEach(item => {
      const cand = item.candidate || item;
      const ev = item.evaluation || item;
      const li = document.createElement("li");
      li.className = "card";

      const bits = [
        '<span class="tag tag-cand">相关备选</span>',
        '<span class="tag">' + escapeHtml(ev.match || item.match_level || "partial") + '</span>'
      ];
      const authorName = cand.author || item.author;
      if (authorName) bits.push('<span class="tag">' + escapeHtml(authorName) + '</span>');

      const gaps = [];
      (ev.criteria_results || []).forEach(cr => {
        if (cr.status !== "supported") {
          gaps.push(cr.criterion_id + " (" + cr.status + ")");
        }
      });
      const gapText = gaps.length ? "主要差距：准则 " + gaps.join("、") + " 未达到强匹配或缺乏确凿证据" : "说明质量或观察项有待完善";
      const skillName = cand.name || cand.skill_id || item.name || item.skill_id || "未命名备选";
      const skillUrl = safeLink(cand.url || cand.repo_url || item.url || item.repo_url);
      const summaryText = ev.summary_zh || item.summary || "相关备选技能";
      const candId = cand.skill_id || item.skill_id;

      li.innerHTML =
        '<div class="card-head">' +
          '<h3><a href="' + escapeHtml(skillUrl) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(skillName) + '</a></h3>' +
        '</div>' +
        '<div class="card-tags" style="margin: 0 0 8px;">' + bits.join("") + '</div>' +
        '<p class="summary">' + escapeHtml(summaryText) + '</p>' +
        '<div class="find-gap-text">⚠️ ' + escapeHtml(gapText) + '</div>';
      container.appendChild(li);
    });
  }
}

function safeLink(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : "#";
  } catch {
    return "#";
  }
}
