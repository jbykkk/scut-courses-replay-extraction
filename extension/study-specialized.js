/* Rendering for two conversation tools; source URLs always come from the local index. */
(() => {
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
  function safeReplay(value) {
    try { const u = new URL(value); return u.protocol === "https:" && ["video.jw.scut.edu.cn", "video-jw-443.webvpn.scut.edu.cn"].includes(u.host) && !u.username && !u.password && u.pathname === "/livingroom" && ["course_id", "sub_id", "note_play"].every(k => /^\d+$/.test(u.searchParams.get(k) || "")) ? u.href : ""; } catch { return ""; }
  }
  function replayLink(where) {
    const url = safeReplay(where?.playback_url); if (!url) return null;
    const a = el("a", "回看 " + where.time_label + " ↗", "replay-link"); a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
    a.title = "在百步梯打开这节课；如需登录，请先完成学校登录。"; return a;
  }
  function renderMentions(root, matches, button) {
    if (!matches?.length) return;
    const section = el("section", undefined, "classroom-matches"); section.append(el("h3", "课堂原句"), el("p", "摘自已保存字幕，识别可能有误；可回看核对。", "fine"));
    for (const item of matches) {
      const card = el("article", undefined, "classroom-quote"), header = el("div", undefined, "quote-heading");
      header.append(el("strong", item.lesson), el("span", item.time_label, "quote-time"));
      card.append(header, el("blockquote", item.quote));
      const actions = el("div", undefined, "quote-actions"), replay = replayLink(item);
      if (replay) actions.append(replay); else actions.append(el("span", item.label, "fine"));
      actions.append(button("前后文", () => StudyTools.showEvidence(item.evidence_id), "quiet")); card.append(actions); section.append(card);
    }
    root.append(section);
  }
  function renderDaily(root, plan, button, start) {
    if (!plan) return;
    const box = el("section", undefined, "daily-agenda");
    box.append(el("div", plan.date + " · " + (plan.course || "全部课程"), "fine"), el("h3", `${plan.used_minutes} 分钟学习${plan.buffer_minutes ? ` · 留 ${plan.buffer_minutes} 分钟机动` : ""}`));
    for (const step of plan.steps) {
      const row = el("article", undefined, "daily-step"), time = el("div", `${step.from_minute}–${step.to_minute}`, "daily-time"); time.append(el("small", "分钟"));
      const body = el("div"); body.append(el("span", step.course + " · " + ({task:"任务",review:"间隔复习",practice:"上次的卡点",diagnostic:"先试一道题"}[step.kind] || "学习"), "fine"), el("h4", step.title));
      body.append(el("p", step.action), el("p", step.reasons.join(" · "), "daily-reason"), button(step.kind === "task" ? "拆出下一步" : "开始这一项", () => start(step), "agenda-start"));
      row.append(time, body); box.append(row);
    }
    for (const warning of plan.warnings || []) box.append(el("p", warning, "daily-reason"));
    if (plan.deferred?.length || plan.needs_source_check?.length) {
      const rest = el("details"); rest.append(el("summary", "这次没排入的内容"));
      for (const s of plan.deferred || []) rest.append(el("p", `${s.course} · ${s.title}：这次时间不够，保留在原任务或复习记录中。`));
      for (const s of plan.needs_source_check || []) rest.append(el("p", `${s.course} · ${s.title}：${s.reason}`)); box.append(rest);
    }
    box.append(el("p", plan.basis, "fine"));
    box.append(button("按当前进度重排", () => start({kind:"replan", minutes:plan.budget_minutes, course:plan.course}), "quiet"));
    if (plan.busy_today?.length) box.append(el("p", "今天已登记的上课时段：" + plan.busy_today.map(b => b.start.slice(11,16) + "–" + b.end.slice(11,16)).join("、"), "fine"));
    root.append(box);
  }
  globalThis.StudySpecialized = {safeReplay, replayLink, renderMentions, renderDaily};
  if (typeof module !== "undefined") module.exports = globalThis.StudySpecialized;
})();
