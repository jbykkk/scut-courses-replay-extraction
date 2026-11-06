(() => {
  "use strict";
  const $ = id => document.getElementById(id), A = AssistantClient, T = StudyTools;
  const post = (path, body = {}) => A.api("/api/study" + path, {method: "POST", body, timeout: 155000});
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
  let home = {turns: [], reviews: [], due: []}, pending = [], sending = false, uploading = 0, lastRender = "";
  const conversation = "main";
  const outboxKey = "study-pending-requests";
  let outbox = [];
  try { outbox = JSON.parse(sessionStorage.getItem(outboxKey) || "[]").filter(item => item.payload?.conversation === conversation && item.payload.client_id); } catch { /* Start with an empty outbox if storage was damaged. */ }
  function persistOutbox() { sessionStorage.setItem(outboxKey, JSON.stringify(outbox)); }
  function draftSnapshot() { return {text: $("question").value, course: $("course").value || sessionStorage.getItem("study-draft-course") || "", attachments: pending.map(file => file.id)}; }
  function hasDraft() { return !!$("question").value.trim() || pending.length > 0; }
  function completeRequest(entry) {
    if (entry.draft && JSON.stringify(draftSnapshot()) === JSON.stringify(entry.draft)) {
      $("question").value = ""; pending = []; renderAttachments();
      sessionStorage.removeItem("study-draft-course");
    }
    sessionStorage.setItem("study-draft-text", $("question").value);
    outbox = outbox.filter(item => item.payload.client_id !== entry.payload.client_id); persistOutbox();
  }
  const niceTime = value => new Date(value * 1000).toLocaleString("zh-CN", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false});
  function notice(text) { $("notice").textContent = text; $("notice").hidden = !text; }
  function button(text, handler, cls = "") {
    const b = el("button", text, cls); b.type = "button";
    b.addEventListener("click", async () => { b.disabled = true; try { await handler(); } catch (e) { notice(e.message); } finally { b.disabled = false; } }); return b;
  }
  function scrollLatest() { $("composer-anchor").scrollIntoView({behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "end"}); }
  function cite(parent, citations) { for (const [i, c] of (citations || []).entries()) { const b = button(c.location ? `${c.location.lesson} · ${c.location.time_label}` : "依据 " + (i + 1) + " ↗", () => T.showEvidence(c.evidence_id), "cite"); b.title = c.quote; parent.append(b); const replay = StudySpecialized.replayLink(c.location); if (replay) parent.append(replay); } }
  function menuClose() { $("tools-menu").close(); }
  $("open-tools").addEventListener("click", () => $("tools-menu").showModal());
  $("close-tools").addEventListener("click", menuClose);
  document.querySelectorAll("[data-open]").forEach(b => b.addEventListener("click", () => { menuClose(); T.switchView(b.dataset.open); }));
  $("close-review").addEventListener("click", () => $("review-dialog").close());
  document.addEventListener("study-open-image", e => { $("evidence-dialog").close(); openImage(e.detail).catch(error => notice(error.message)); });
  $("manage-reviews").addEventListener("click", () => { menuClose(); renderReviews(); $("review-dialog").showModal(); });
  document.addEventListener("study-state", e => {
    const s = e.detail;
    const remembered = sessionStorage.getItem("study-draft-course");
    if (hasDraft() && remembered !== null && [...$("course").options].some(option => option.value === remembered)) $("course").value = remembered;
    if (!hasDraft() && !$("course").value && home.course) $("course").value = home.course;
    $("model-note").textContent = s.provider.configured
      ? `${s.provider.model}${s.provider.vision ? " · 支持图片" : " · 当前仅文字"} · 发送时使用相关摘录与所选图片，记录保存在本机`
      : "模型尚未连接。可以先发送资料，或到设置中保存 Key。";
    $("onboarding").textContent = s.sources.length ? `已索引 ${s.sources.length} 份资料与课堂记录。` : "添加资料或同步课堂记录后，可以按课程提问。";
  });
  $("attach").addEventListener("click", () => $("attachment-picker").click());
  $("course").addEventListener("change", () => sessionStorage.setItem("study-draft-course", $("course").value));
  $("start-attachment").addEventListener("click", () => $("attachment-picker").click());
  $("attachment-picker").addEventListener("change", e => { addFiles([...e.target.files]); e.target.value = ""; });
  $("question").addEventListener("paste", e => { const files = [...(e.clipboardData?.items || [])].filter(x => x.kind === "file").map(x => x.getAsFile()).filter(Boolean); if (files.length) { e.preventDefault(); addFiles(files); } });
  const shell = document.querySelector(".conversation-shell");
  shell.addEventListener("dragover", e => { if ([...e.dataTransfer.types].includes("Files")) { e.preventDefault(); shell.classList.add("dragging"); } });
  shell.addEventListener("dragleave", e => { if (!shell.contains(e.relatedTarget)) shell.classList.remove("dragging"); });
  shell.addEventListener("drop", e => { e.preventDefault(); shell.classList.remove("dragging"); addFiles([...e.dataTransfer.files]); });
  function renderAttachments() {
    const root = $("attachment-list"); root.replaceChildren();
    for (const file of pending) root.append(button((file.image ? "▧ " : "▤ ") + file.name + "  ×", async () => { pending = pending.filter(f => f.id !== file.id); renderAttachments(); }, "attachment-chip"));
    if (uploading) root.append(el("span", "正在读取附件…", "fine"));
    sessionStorage.setItem("study-draft-files", JSON.stringify(pending));
    if (pending.length) sessionStorage.setItem("study-draft-course", draftSnapshot().course);
    $("send").disabled = sending || uploading > 0;
  }
  async function addFiles(files) {
    if (sending) { notice("这一轮正在阅读，等它回答后再补充附件。"); return; }
    if (pending.length + files.length + uploading > 4) { notice("一次可以发 4 份资料，先选最相关的部分。"); return; }
    uploading += files.length; renderAttachments();
    for (const file of files) {
      try {
        if (file.size > 12 * 1024 * 1024) throw new Error("单份文件请控制在 12 MB 内。");
        const content = await new Promise((resolve, reject) => { const r = new FileReader(); r.onload = () => resolve(r.result.split(",")[1]); r.onerror = reject; r.readAsDataURL(file); });
        const item = await post("/attachments", {name: file.name, content});
        if (!pending.some(p => p.id === item.id)) pending.push(item);
      } catch (e) { notice(file.name + "：" + (e.message || "读取失败")); }
      finally { --uploading; renderAttachments(); }
    }
    $("question").focus();
  }
  async function openImage(id) {
    const {token} = await A.connection();
    const response = await fetch(A.base + "/api/study/attachments/" + encodeURIComponent(id) + "/image", {headers: {Authorization: "Bearer " + token}});
    if (!response.ok) throw new Error("原图暂时不可用");
    const url = URL.createObjectURL(await response.blob());
    $("evidence-title").textContent = "发送的原图"; $("evidence-location").textContent = "对照模型识读结果检查，尤其留意公式、数字与模糊文字。";
    $("evidence-text").textContent = ""; const img = el("img"); img.src = url; img.alt = "课程原图"; img.className = "source-image"; $("evidence-text").append(img);
    $("evidence-dialog").showModal(); $("evidence-dialog").addEventListener("close", () => URL.revokeObjectURL(url), {once: true});
  }
  async function refresh(force = false) {
    const nearEnd = window.innerHeight + window.scrollY > document.body.scrollHeight - 260;
    home = await A.api("/api/study/home?" + new URLSearchParams({conversation})); renderNudge();
    for (const entry of [...outbox]) {
      if (home.turns.some(turn => turn.status === "ready" && turn.request_input?.client_id === entry.payload.client_id)) completeRequest(entry);
    }
    if (!hasDraft() && !$("course").value && home.course && [...$("course").options].some(o => o.value === home.course)) $("course").value = home.course;
    const signature = JSON.stringify(home.turns);
    StudyActions.updateJobs(home.operations || []);
    if (signature !== lastRender || force) { renderDialogue(); lastRender = signature; if (nearEnd && !force) scrollLatest(); }
    $("pending-badge").textContent = home.pending_count || "";
  }
  function renderDialogue() {
    const root = $("conversation"); root.replaceChildren(); $("welcome").hidden = home.turns.length > 0;
    for (const turn of home.turns) {
      const pair = el("article", undefined, "exchange"), user = el("div", undefined, "user-message");
      if (turn.course) user.append(el("span", turn.course, "course-label")); user.append(el("p", turn.question));
      for (const attachment of turn.attachments || []) user.append(attachment.image ? button("▧ " + attachment.name, () => openImage(attachment.id), "attachment-chip") : el("span", "▤ " + attachment.name, "attachment-chip"));
      pair.append(user); const reply = el("div", undefined, "assistant-message"); reply.append(el("span", "SCUT", "speaker"));
      if (turn.status === "failed") reply.append(el("p", turn.error, "uncertainty"), button("接着试一次", () => send(turn.question, {request: turn.request_input || {
        conversation: turn.conversation || conversation, client_id: "retry-" + turn.id, question: turn.question,
        course: turn.course, attachments: (turn.attachments || []).map(a => a.id), purpose: turn.purpose, review_id: turn.review_id || null
      }})));
      else if (turn.status === "running") reply.append(el("p", "这条消息正在处理。页面关掉再回来，对话仍会留在这里。", "fine"));
      else renderAnswer(reply, turn);
      const traceId=turn.trace_id||turn.result?.trace_id;
      if(traceId)reply.append(button("查看执行过程 ↗",()=>document.dispatchEvent(new CustomEvent("study-runtime",{detail:traceId})),"runtime-inspect-link"));
      pair.append(reply); root.append(pair);
    }
  }
  function renderAnswer(root, turn) {
    const r = turn.result || {}; if (r.reply && !r.operation_id) root.append(el("p", r.reply));
    StudySpecialized.renderDaily(root, r.daily_plan, button, step => {
      if (step.kind === "replan") return send(`今天${step.course ? "只安排" + step.course + "，" : ""}还有 ${step.minutes} 分钟，按当前进度重新安排复习清单。`, {course:step.course, purpose:"today"});
      if (step.kind === "review") return startReview({...step, topic:step.title});
      return send(step.kind === "task" ? `我现在留出 ${step.minutes} 分钟做“${step.title}”。结合已确认的要求，帮我拆出可以动手的下一步。不要直接给作业答案。` : `请围绕“${step.title}”给一道题，检验上次没弄清的地方。`, {course:step.course, purpose:step.kind === "task" ? "ask" : "practice"});
    });
    if (r.status === "needs_course") {
      const choices = el("div", undefined, "suggestions");
      for (const name of r.choose_course || []) choices.append(button(name, () => send(turn.question, {course: name, attachments: turn.attachments.map(a => a.id)})));
      const name = el("input"); name.placeholder = "也可以直接写课程名称"; name.maxLength = 160; name.setAttribute("aria-label", "新课程名称");
      choices.append(name, button("接着看这门课", () => { if (!name.value.trim()) throw new Error("写下课程名就好"); return send(turn.question, {course: name.value.trim(), attachments: turn.attachments.map(a => a.id)}); })); root.append(choices); return;
    }
    if (r.operation_id) { const node=el("div"); node.dataset.operationId=r.operation_id; root.append(node); const job=(home.operations||[]).find(j=>j.id===r.operation_id); if(job)StudyActions.renderJob(node,job); }
    if (r.input_images) root.append(el("p", "已结合截图识读 · 公式和数字可点上方原图核对", "image-reading-label"));
    let extra;
    for (const [index, claim] of (r.claims || []).entries()) { const row = el("div", undefined, "answer-point"); row.append(el("p", claim.text)); cite(row, claim.citations);
      if(claim.verification)row.append(el("p",claim.verification.label,"verification-note"));
      const memory=el("details",undefined,"remember-actions");memory.append(el("summary","保存这条理解"),button("留待核对",()=>post("/knowledge",{turn_id:turn.id,claim_index:index,status:"pending"}).then(()=>notice("已加入核对记录"))));
      if(r.support_status==="checked"&&claim.verification?.status!=="needs_check")memory.append(button("我已对照原文确认",()=>post("/knowledge",{turn_id:turn.id,claim_index:index,status:"confirmed"}).then(()=>notice("已保存；来源更新后会暂停引用"))));
      row.append(memory);
      if (index > 1 && !["summary", "connect"].includes(turn.purpose)) { if (!extra) { extra = el("details", undefined, "more-explanation"); extra.append(el("summary", "继续展开讲讲")); root.append(extra); } extra.append(row); } else root.append(row);
    }
    for (const link of r.connections || []) { const row = el("div", undefined, "knowledge-link"); row.append(el("strong", link.concept_a + "  ↔  " + link.concept_b), el("p", link.relationship)); cite(row, link.citations); root.append(row); }
    StudySpecialized.renderMentions(root, r.classroom_matches, button);
    if (r.uncertainties?.length) { const details = el("details", undefined, "caveats"); if (!r.claims?.length && !r.exercise) details.open = true; details.append(el("summary", "还需要核对的地方 · " + r.uncertainties.length)); for (const text of r.uncertainties) details.append(el("p", text)); root.append(details); }
    if (r.proposals?.length) { const p = r.proposals[0], card = el("div", undefined, "inline-step"); card.append(el("span", "待确认事项", "fine"), el("h3", p.title), el("p", p.detail), button("核对并保存", () => proposalSteps(card, p, turn.course))); root.append(card); }
    if (r.exercise) renderExercise(root, r.exercise, turn);
    if (r.next_question) root.append(el("p", r.next_question, "next-question"));
    if ((r.claims || []).length || r.exercise) {
      const actions = el("div", undefined, "suggestions");
      actions.append(button("串联前后知识", () => send("结合刚才的内容和这门课的前后知识，解释它们为什么有关、在哪些条件下不同。请给出两处依据。", {course: turn.course, purpose: "connect"})),
        button("深入总结一下", () => send("围绕刚才的主题深入总结：核心问题、推导关系、适用边界与容易混淆的地方。", {course: turn.course, purpose: "summary"})),
        button("加入复习", async () => { const review = await post("/reviews", {turn_id: turn.id, days: 1}); notice("复习安排已保存，下次复习：" + niceTime(review.due_at) + "。已有作答记录会保留。"); await refresh(); })); root.append(actions);
      const trace = el("details", undefined, "trace"); trace.append(el("summary", "这次参考了什么"), el("p", `结合当前课程与最近对话，执行 ${(r.trace || []).filter(t => t.tool).length} 次来源读取。课程结论可回查上面的依据；图片识读单独标明。`)); root.append(trace);
    }
  }
  function proposalSteps(card, proposal, course) {
    const stage = el("div", undefined, "one-step"); card.replaceChildren(el("h3", proposal.title), stage);
    const save = async (deadline, min, max) => { await post(`/proposals/${proposal.id}`, {status: "accepted", deadline, estimate_min: min, estimate_max: max}); stage.replaceChildren(el("p", "已记下。后续交流和安排会参考这条要求。")); await T.load(); await refresh(); };
    if (proposal.proposal_kind !== "task") { stage.append(el("p", proposal.detail), button("记住这个要求", () => save(null, null, null))); return; }
    let deadline = null;
    const effort = () => {
      stage.replaceChildren(el("p", "这件事大约需要多久？不确定时，可以先拆出今天能动手的一小步。"));
      for (const [min, max] of [[20, 40], [60, 90], [120, 180]]) stage.append(button(`${min}–${max} 分钟`, () => save(deadline, min, max)));
      stage.append(button("还不好估计，帮我拆小一点", () => send("这项任务是：“" + proposal.title + "”。请结合已知要求拆成最先能动手的一小步。不要替我确认截止日期或总耗时。", {course, purpose: "requirements"})));
      const more = el("details"), input = el("input"); input.type = "number"; input.min = "5"; input.max = "12000"; input.setAttribute("aria-label", "自己估计的分钟数"); more.append(el("summary", "自己填写预计分钟"), input, button("按这个时间记下", () => { const n = Number(input.value); if (n < 5 || n > 6000) throw new Error("请填写 5–6000 分钟，也可以先拆小任务"); return save(deadline, n, n); })); stage.append(more);
    };
    const input = el("input"); input.type = "date"; input.setAttribute("aria-label", "已经确认的截止日期");
    stage.append(el("p", proposal.due_text ? "老师提到的时间是“" + proposal.due_text + "”。你能确认对应哪一天吗？" : "先补最有用的一项：截止时间清楚吗？"), input,
      button("就是这一天", () => { if (!input.value) throw new Error("先选一个已确认的日期，或选暂不确定"); deadline = input.value; effort(); }), button("暂不确定，先留空", async () => effort()));
  }
  function renderExercise(root, exercise, turn) {
    const box = el("div", undefined, "exercise"), form = el("form"), answer = el("textarea"), rating = el("select"), result = el("div");
    box.append(el("span", "练习", "fine"), el("h3", exercise.question));
    if (exercise.last_attempt) { box.append(el("p", "你当时的回答：" + exercise.last_attempt.answer), el("p", exercise.expected_answer)); cite(box, exercise.citations); root.append(box); return; }
    answer.required = true; answer.maxLength = 3000; answer.rows = 3; answer.placeholder = "写下你的思路，卡在哪里也可以…"; answer.setAttribute("aria-label", "这道题的回答");
    for (const [i, text] of ["完全卡住", "需要一点提示", "能大致解释", "能独立推导"].entries()) { const option = el("option", text); option.value = i + 1; rating.append(option); }
    rating.setAttribute("aria-label", "这次回忆的感受"); const submit = el("button", "看看参考答案"); submit.type = "submit";
    form.append(answer, rating, submit); form.addEventListener("submit", async e => { e.preventDefault(); submit.disabled = true; try {
      const reply = await post(`/practice/${exercise.id}/attempt`, {answer: answer.value, rating: Number(rating.value), review_id: turn.review_id || null});
      result.replaceChildren(el("p", reply.expected_answer)); cite(result, reply.citations); const feedback=reply.attempts?.at(-1)?.feedback;if(feedback){result.append(el("h4","作答反馈"),el("p",feedback.summary));for(const point of feedback.missing||[])result.append(el("p",point));result.append(el("small",feedback.label||"暂按自评安排"));}
      if (turn.review_id) { const review = reply.review || await post(`/reviews/${turn.review_id}`, {action: "complete", rating: Number(rating.value)}); result.append(el("p", "下次复习：" + niceTime(review.due_at), "fine")); await refresh(); }
      else result.append(el("p", "用下方“加入复习”安排这段内容的复习。", "fine"));
    } catch (error) { notice(error.message); submit.disabled = false; } }); box.append(form, result); root.append(box);
  }
  function renderNudge() {
    const root = $("gentle-next"); root.replaceChildren();
    if (home.due.length) { const review = home.due[0]; root.append(el("span", "今日复习", "eyebrow"), el("strong", review.course + " · " + review.topic), button("花几分钟回忆", () => startReview(review)), button("明天再来", async () => { await post(`/reviews/${review.id}`, {action: "snooze"}); await refresh(); }, "quiet")); root.hidden = false; }
    else if (home.task_count) { root.append(el("span", "待办", "eyebrow"), el("p", `还有 ${home.task_count} 件已确认的任务，尚未完成。`), button("帮我安排今天", today)); root.hidden = false; }
    else if (home.reviews.length) { root.append(el("span", "下次复习", "eyebrow"), el("p", `下次复习 ${niceTime(home.reviews[0].due_at)} · ${home.reviews[0].course}`), button("调整节奏", () => { renderReviews(); $("review-dialog").showModal(); }, "quiet")); root.hidden = false; }
    else root.hidden = true;
  }
  function startReview(review) { $("review-dialog").close(); const mode={prerequisite:"先找出前置理解缺口，给一道基础辨析题",application:"换一个情境，给一道应用题",connection:"给一道串联前后概念的题",recall:"给一道回忆关键条件的题"}[review.mode]||"给一道题"; return send(mode+"。"+"请围绕“" + review.topic + "”出一道检验理解的题，接着串联它的前置知识。先不要给答案。", {course: review.course, purpose: "practice", review_id: review.id}); }
  function renderReviews() {
    const root = $("review-list"); root.replaceChildren();
    const paused = home.paused_reviews || [];
    if (!home.reviews.length) root.append(el("p", paused.length ? "当前没有进行中的复习，可以在下方恢复已暂停的条目。" : "在回答下面点击“加入复习”，这里会显示下一次复习时间。"));
    for (const r of home.reviews) { const card = el("div", undefined, "review-row"); card.append(el("span", r.course, "pill"), el("h3", r.topic), el("p", niceTime(r.due_at)+" · 约 "+r.minutes+" 分钟 · "+(r.reasons||[]).join("；"), "fine"), button("现在回忆", () => startReview(r)), button("推迟一天", async () => { await post(`/reviews/${r.id}`, {action: "snooze"}); await refresh(); renderReviews(); }), button("暂停这一项", async () => { await post(`/reviews/${r.id}`, {action: "pause"}); await refresh(); renderReviews(); })); root.append(card); }
    if (paused.length) {
      const section = el("details"); section.open = !home.reviews.length; section.append(el("summary", "已暂停 · " + paused.length));
      for (const r of paused) {
        const card = el("div", undefined, "review-row");
        card.append(el("span", r.course, "pill"), el("h3", r.topic), el("p", `已暂停 · 保留 ${(r.attempts || []).length} 次作答记录`, "fine"), button("恢复复习", async () => {
          const review = await post(`/reviews/${r.id}`, {action: "resume"});
          await refresh(); renderReviews(); notice("已恢复复习，下次复习：" + niceTime(review.due_at));
        })); section.append(card);
      }
      root.append(section);
    }
  }
  async function today() {
    return send("今晚只有30分钟，先学什么？", {course:"", purpose:"today"});
  }
  $("open-today").addEventListener("click", () => today().catch(e => notice(e.message)));
  document.querySelectorAll("[data-starter]").forEach(b => b.addEventListener("click", () => { if (b.dataset.intent === "today") today().catch(e => notice(e.message)); else if (hasDraft()) { notice("输入框中还有草稿，请先发送或整理好当前内容。"); $("question").focus(); } else { $("question").value = b.dataset.starter; sessionStorage.setItem("study-draft-text", $("question").value); $("question").focus(); } }));
  async function send(text, options = {}) {
    if (sending || (options.draft && uploading)) return;
    const question = text.trim(); if (!question) return;
    const payload = options.request || {conversation, question, course: options.course ?? $("course").value,
      attachments: options.attachments || [], purpose: options.purpose || "auto", review_id: options.review_id || null};
    const kind = options.draft ? "draft" : "action";
    const signature = value => JSON.stringify([value.conversation, value.question, value.course, value.attachments, value.purpose, value.review_id]);
    let entry = outbox.find(item => payload.client_id ? item.payload.client_id === payload.client_id : item.kind === kind && signature(item.payload) === signature(payload));
    if (!entry) {
      entry = {kind, payload: {...payload, client_id: payload.client_id || crypto.randomUUID()}, draft: options.draft || null};
      outbox.push(entry); persistOutbox();
    }
    sending = true; $("thinking").hidden = false; $("send").disabled = true; notice("");
    try {
      const result = await post("/chat", entry.payload);
      completeRequest(entry);
      if (result.course && !hasDraft() && !$("course").value) $("course").value = result.course;
      await refresh(true); await T.load(); scrollLatest();
    } catch (e) { notice(e.message + "；草稿已保留，重试会继续同一条请求。"); await refresh().catch(() => {}); }
    finally { sending = false; $("thinking").hidden = true; $("send").disabled = uploading > 0; }
  }
  function submitDraft() {
    const draft = draftSnapshot();
    sessionStorage.setItem("study-draft-course", draft.course);
    const question = draft.text.trim() || (draft.attachments.length ? "帮我读懂这份资料，先提炼一个核心问题，再引导我往下学。" : "");
    return send(question, {draft, course: draft.course, attachments: draft.attachments});
  }
  $("ask-form").addEventListener("submit", e => { e.preventDefault(); void submitDraft(); });
  $("question").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); void submitDraft(); } });
  $("question").addEventListener("input", () => { sessionStorage.setItem("study-draft-text", $("question").value); sessionStorage.setItem("study-draft-course", draftSnapshot().course); $("question").style.height = "auto"; $("question").style.height = Math.min(180, $("question").scrollHeight) + "px"; });
  try { pending = JSON.parse(sessionStorage.getItem("study-draft-files") || "[]").slice(0, 4); $("question").value = sessionStorage.getItem("study-draft-text") || ""; } catch { pending = []; }
  renderAttachments(); refresh().catch(e => notice(e.message));
  setInterval(() => { if (!document.hidden && !sending && !document.querySelector("dialog[open]")) refresh().catch(() => {}); }, 7000);
})();
