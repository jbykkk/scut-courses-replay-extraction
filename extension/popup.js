const {
  SUPPORTED_HOSTS,
  parseCourseUrl,
  createApi,
  extractSubtitleItems,
  toSrt,
  toTxt,
  sanitizeFilename,
  sanitizeIdForFilename,
} = ScutSubtitleCore;

class RequestError extends Error {
  constructor(message, { status = null, kind = "network" } = {}) {
    super(message);
    this.name = "RequestError";
    this.status = status;
    this.kind = kind;
  }
}

async function directJsonRequest(url) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      let response;
      try {
        response = await fetch(url, { credentials: "include", signal: controller.signal });
      } catch (error) {
        throw new RequestError(error.name === "AbortError" ? "请求超时，请重试" : (error.message || "网络请求失败"), { kind: "network" });
      }
      if (!response.ok) {
        const kind = response.status === 401 || response.status === 403 ? "auth" : "http";
        throw new RequestError(`HTTP ${response.status}`, { status: response.status, kind });
      }
      console.info(`[SCUT] 接口响应：${url} status=${response.status}`);
      const contentType = response.headers.get("content-type") || "";
      const text = await response.text();
      try {
        return JSON.parse(text);
      } catch {
        throw new RequestError(
          `接口正文不是 JSON，可能需要重新登录（Content-Type: ${contentType || "未知"}）`,
          { kind: "auth" }
        );
      }
    } catch (error) {
      if (!(error instanceof RequestError)) {
        error = new RequestError(error.name === "AbortError" ? "请求超时，请重试" : (error.message || "网络请求失败"), { kind: "network" });
      }
      const retryable = error.kind === "network" || [408, 429].includes(error.status) || error.status >= 500;
      if (attempt === 0 && retryable) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        continue;
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }
}

async function pageJsonRequest(tabId, expectedOrigin, url) {
  const tab = await chrome.tabs.get(tabId);
  const parsed = parseCourseUrl(tab.url || "");
  if (!parsed || parsed.origin !== expectedOrigin) {
    throw new RequestError("课程标签页已经切换，请回到原课程页面后重试", { kind: "page" });
  }

  let injection;
  try {
    injection = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: async (requestUrl, origin, supportedHosts) => {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 20000);
        try {
          const current = new URL(location.href);
          const target = new URL(requestUrl);
          if (
            location.origin !== origin ||
            target.origin !== origin ||
            !supportedHosts.includes(current.hostname)
          ) {
            return { ok: false, kind: "page", message: "页面来源校验失败" };
          }
          const response = await fetch(target.href, { credentials: "include", signal: controller.signal });
          const contentType = response.headers.get("content-type") || "";
          if (!response.ok) {
            return {
              ok: false,
              kind: response.status === 401 || response.status === 403 ? "auth" : "http",
              status: response.status,
              message: `HTTP ${response.status}`,
            };
          }
          const text = await response.text();
          try {
            return { ok: true, data: JSON.parse(text) };
          } catch {
            return {
              ok: false,
              kind: "auth",
              message: `接口正文不是 JSON，可能需要重新登录（Content-Type: ${contentType || "未知"}）`,
            };
          }
        } catch (error) {
          return { ok: false, kind: "network", message: error.name === "AbortError" ? "请求超时，请重试" : (error.message || "页面内请求失败") };
        } finally {
          clearTimeout(timer);
        }
      },
      args: [url, expectedOrigin, Array.from(SUPPORTED_HOSTS)],
    });
  } catch (error) {
    throw new RequestError(`无法在课程页面内请求：${error.message}`, { kind: "permission" });
  }

  const result = injection?.[0]?.result;
  if (!result) throw new RequestError("课程页面没有返回请求结果", { kind: "page" });
  if (!result.ok) {
    throw new RequestError(result.message, { status: result.status, kind: result.kind });
  }
  return result.data;
}

function shouldFallback(error) {
  return error instanceof RequestError && ["network", "auth"].includes(error.kind);
}

async function requestJson(url, pageContext) {
  console.info(`[SCUT] 请求接口：${url}`);
  try {
    return await directJsonRequest(url);
  } catch (error) {
    if (!shouldFallback(error)) throw error;
    console.info(`[SCUT] 弹窗请求失败（${error.message}），改用课程页面内请求`);
    return pageJsonRequest(pageContext.tabId, pageContext.origin, url);
  }
}

function humanizeError(error) {
  if (error?.kind === "auth" || error?.status === 401 || error?.status === 403) {
    return "登录状态已失效，请重新登录 WebVPN/华园视频";
  }
  if (error?.kind === "permission") return "插件权限不足，请重新加载扩展后重试";
  if (error?.status === 404) return "接口不存在或课程链接已失效（HTTP 404）";
  if (error?.kind === "page") return error.message;
  return error?.message || "网络或接口请求失败";
}

function checkApiResult(body) {
  for (const result of [body?.result, body]) {
    if (!result || typeof result !== "object") continue;
    if (result.success === false || (result.code != null && !["0", "200"].includes(String(result.code)))) {
      throw new Error(result.msg || result.message || (result.code != null ? `接口错误 ${result.code}` : "接口返回失败"));
    }
  }
}

async function loadSubtitle(url, pageContext) {
  const body = await requestJson(url, pageContext);
  // 10002 是平台明确返回的“未查询到语音数据”。
  if (String(body?.code) !== "10002") checkApiResult(body);
  const items = extractSubtitleItems(body);
  return {
    items,
    reason: items.length ? "" : (String(body?.code) === "10002" ? body.msg : "平台暂无字幕数据，可能尚未生成"),
  };
}

function classifyLesson(lesson, now = Date.now()) {
  const start = Number(lesson.start_at);
  const startMs = start < 1e12 ? start * 1000 : start;
  if (Number.isFinite(startMs) && startMs > now) return "upcoming";
  // 平台状态：6 可回放，1 直播中，2 未开始，3 回放生成中，5 无回放。
  switch (String(lesson.status)) {
    case "6": return "replay";
    case "2": return "upcoming";
    case "1": return "live";
    default: return "unavailable";
  }
}

function startDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  return new Promise((resolve, reject) => {
    chrome.downloads.download({ url, filename, saveAs: false }, (downloadId) => {
      const error = chrome.runtime.lastError;
      URL.revokeObjectURL(url);
      if (error || downloadId === undefined) {
        reject(
          new RequestError(
            `浏览器未能创建下载任务：${error?.message || "未知错误"}`,
            { kind: "permission" }
          )
        );
        return;
      }
      resolve(downloadId);
    });
  });
}

function downloadFile(content, filename) {
  const type = filename.endsWith(".json")
    ? "application/json;charset=utf-8"
    : filename.endsWith(".srt")
      ? "application/x-subrip;charset=utf-8"
      : "text/plain;charset=utf-8";
  return startDownload(new Blob([content], { type }), filename);
}

async function downloadZip(files, zipName) {
  const zip = new JSZip();
  for (const { name, content } of files) zip.file(name, content);
  const blob = await zip.generateAsync({ type: "blob" });
  return startDownload(blob, zipName);
}

function showStatus(id, text, type) {
  const element = document.getElementById(id);
  element.textContent = text;
  element.className = `status ${type}`;
}

document.addEventListener("DOMContentLoaded", async () => {
  const mainEl = document.getElementById("main");
  const errorEl = document.getElementById("error-page");

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const parsed = parseCourseUrl(tab?.url || "");

  if (!parsed || tab.id === undefined) {
    errorEl.classList.remove("hidden");
    return;
  }

  const api = createApi(parsed.origin);
  const pageContext = { tabId: tab.id, origin: parsed.origin };
  const { subId, courseId } = parsed;
  mainEl.classList.remove("hidden");
  document.getElementById("site-name").textContent =
    parsed.siteKey === "external" ? "校外 WebVPN" : "校内站点";

  let courseTitle = null;
  let catalogue = [];
  let lastReport = null;
  const lessonResults = new Map();

  async function safeRequest(url) {
    try {
      const body = await requestJson(url, pageContext);
      checkApiResult(body);
      return body;
    } catch (error) {
      console.warn(`[SCUT] 可选信息获取失败：${url}`, error);
      return null;
    }
  }

  // === Load course info (single tab) ===
  if (courseId) {
    const [infoBody, titleBody] = await Promise.all([
      safeRequest(api.subInfo(courseId, subId)),
      safeRequest(api.courseTitle(courseId)),
    ]);
    const info = infoBody?.data || null;
    const title = titleBody?.data?.[0]?.course_title || null;
    courseTitle = title;
    if (title) document.getElementById("course-name").textContent = title;
    if (info) {
      document.getElementById("lecturer-name").textContent = info.lecturer_name || "-";
      document.getElementById("sub-title").textContent = info.sub_title || "-";
      document.getElementById("course-subtitle").textContent =
        `${info.lecturer_name || ""} · ${info.sub_title || subId}`;
    } else {
      document.getElementById("sub-title").textContent = subId;
      document.getElementById("course-subtitle").textContent = `sub_id: ${subId}`;
    }
  } else {
    document.getElementById("sub-title").textContent = subId;
    document.getElementById("course-subtitle").textContent = `sub_id: ${subId}`;
  }

  // === Tab switching ===
  document.querySelectorAll(".tab").forEach((tabBtn) => {
    tabBtn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".tab-content").forEach((t) => t.classList.remove("active"));
      tabBtn.classList.add("active");
      document.getElementById(`tab-${tabBtn.dataset.tab}`).classList.add("active");
    });
  });

  // === Single lesson download ===
  document.getElementById("download-btn").addEventListener("click", async () => {
    const wantSrt = document.getElementById("fmt-srt").checked;
    const wantTxt = document.getElementById("fmt-txt").checked;
    const wantJson = document.getElementById("fmt-json").checked;
    if (!wantSrt && !wantTxt && !wantJson) {
      showStatus("status-single", "请至少选择一种格式", "error");
      return;
    }

    const btn = document.getElementById("download-btn");
    btn.disabled = true;
    showStatus("status-single", "正在获取字幕...", "loading");

    try {
      const { items, reason } = await loadSubtitle(api.subtitle(subId), pageContext);
      if (items.length === 0) {
        showStatus("status-single", `该课时暂无字幕：${reason}`, "warning");
        return;
      }
      if (wantSrt) await downloadFile(toSrt(items), `subtitle_${subId}.srt`);
      if (wantTxt) await downloadFile(toTxt(items), `subtitle_${subId}.txt`);
      if (wantJson) await downloadFile(JSON.stringify(items, null, 2), `subtitle_${subId}.json`);
      showStatus("status-single", `已下载 ${items.length} 条字幕`, "success");
    } catch (err) {
      showStatus("status-single", humanizeError(err), "error");
    } finally {
      btn.disabled = false;
    }
  });

  // === Batch: load catalogue ===
  let catalogueError = null;
  const excluded = { upcoming: 0, live: 0, unavailable: 0 };
  if (courseId) {
    try {
      const body = await requestJson(api.catalogue(courseId), pageContext);
      checkApiResult(body);
      const lessons = body?.result?.data;
      if (!Array.isArray(lessons)) throw new Error("课程目录格式异常，请重试");
      const now = Date.now();
      const seen = new Set();
      catalogue = lessons.filter((lesson) => {
        let sid;
        try {
          sid = ScutSubtitleCore.normalizeNumericId(lesson.sub_id, "sub_id");
        } catch {
          console.warn("[SCUT] 跳过包含非法 sub_id 的课程目录项");
          return false;
        }
        if (seen.has(sid)) return false;
        seen.add(sid);
        const state = classifyLesson(lesson, now);
        if (state === "replay") return true;
        excluded[state]++;
        return false;
      }).map((lesson) => ({ ...lesson, sub_id: String(lesson.sub_id) }));
    } catch (err) {
      catalogueError = humanizeError(err);
    }
  }

  const listEl = document.getElementById("lesson-list");
  const countEl = document.getElementById("lesson-count");
  const selectAll = document.getElementById("select-all");
  const batchBtn = document.getElementById("batch-download-btn");
  function updateCourseSummary() {
    const problems = Array.from(lessonResults.values()).filter((result) => result.status !== "exported").length;
    let text = catalogueError ? "" : `已识别 ${catalogue.length} 节课`;
    if (problems > 0 || (catalogue.length > 0 && lessonResults.size === catalogue.length)) {
      text += ` · ${problems} 节异常`;
    }
    document.getElementById("catalogue-note").textContent = text;
  }
  updateCourseSummary();

  if (catalogue.length === 0) {
    countEl.textContent = catalogueError ? "课程目录加载失败" : "当前没有可导出的回放";
    selectAll.checked = false;
    selectAll.disabled = true;
    batchBtn.disabled = true;
    if (catalogueError) showStatus("status-batch", catalogueError, "error");
  } else {
    batchBtn.disabled = false;
    countEl.textContent = `已选 ${catalogue.length} / ${catalogue.length} 次回放`;

    catalogue.forEach((lesson) => {
      const item = document.createElement("div");
      item.className = "lesson-item";

      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "lesson-check";
      checkbox.dataset.subId = lesson.sub_id;
      checkbox.checked = true;

      const label = document.createElement("span");
      label.className = "lesson-title";
      label.textContent = lesson.title || lesson.sub_id;

      item.appendChild(checkbox);
      item.appendChild(label);
      const result = document.createElement("span");
      result.className = "lesson-result";
      result.dataset.subId = lesson.sub_id;
      item.appendChild(result);
      listEl.appendChild(item);
    });

    selectAll.addEventListener("change", () => {
      listEl.querySelectorAll(".lesson-check").forEach((c) => (c.checked = selectAll.checked));
      updateCount();
    });
    listEl.addEventListener("change", (e) => {
      if (e.target.classList.contains("lesson-check")) updateCount();
    });
  }

  function getSelectedLessons() {
    return Array.from(listEl.querySelectorAll(".lesson-check:checked")).map(
      (c) => c.dataset.subId
    );
  }

  function updateCount() {
    const total = catalogue.length;
    const selected = getSelectedLessons().length;
    countEl.textContent = `已选 ${selected} / ${total} 次回放`;
    selectAll.checked = total > 0 && selected === total;
    selectAll.indeterminate = selected > 0 && selected < total;
  }

  document.getElementById("report-download-btn").addEventListener("click", async () => {
    if (!lastReport) return;
    try {
      await downloadFile(JSON.stringify(lastReport, null, 2), "字幕导出结果.json");
    } catch (err) {
      showStatus("status-batch", `报告下载失败：${err.message}`, "error");
    }
  });

  // === Batch download ===
  document.getElementById("batch-download-btn").addEventListener("click", async () => {
    const selectedSubIds = getSelectedLessons();
    if (selectedSubIds.length === 0) {
      showStatus("status-batch", "请至少选择一节课", "error");
      return;
    }

    const wantSrt = document.getElementById("batch-fmt-srt").checked;
    const wantTxt = document.getElementById("batch-fmt-txt").checked;
    const wantJson = document.getElementById("batch-fmt-json").checked;
    if (!wantSrt && !wantTxt && !wantJson) {
      showStatus("status-batch", "请至少选择一种格式", "error");
      return;
    }

    const btn = document.getElementById("batch-download-btn");
    btn.disabled = true;
    const controls = document.querySelectorAll("#tab-batch input");
    controls.forEach((control) => (control.disabled = true));
    const reportBtn = document.getElementById("report-download-btn");
    reportBtn.classList.add("hidden");
    const details = document.getElementById("batch-details");
    details.classList.add("hidden");
    lastReport = null;
    const progressBar = document.getElementById("batch-progress-bar");
    const progressFill = document.getElementById("batch-progress-fill");
    progressFill.style.width = "0%";
    progressBar.classList.remove("hidden");
    showStatus("status-batch", `正在获取 0/${selectedSubIds.length}...`, "loading");

    const files = [];
    const results = [];
    let successLessons = 0;
    let noSubtitles = 0;
    let failed = 0;

    try {
      for (const resultEl of listEl.querySelectorAll(".lesson-result")) resultEl.textContent = "";
      for (let i = 0; i < selectedSubIds.length; i++) {
        const sid = selectedSubIds[i];
        const lesson = catalogue.find((l) => l.sub_id === sid);
        const label = sanitizeFilename(lesson?.title || sid);
        const safeSid = sanitizeIdForFilename(sid, "sub_id");
        const prefix = `${String(i + 1).padStart(2, "0")}_${label}_${safeSid}`;
        const result = { sub_id: sid, title: lesson?.title || sid };

        try {
          const { items, reason } = await loadSubtitle(api.subtitle(sid), pageContext);
          if (items.length > 0) {
            const lessonFiles = [];
            if (wantSrt) lessonFiles.push({ name: `${prefix}.srt`, content: toSrt(items) });
            if (wantTxt) lessonFiles.push({ name: `${prefix}.txt`, content: toTxt(items) });
            if (wantJson) lessonFiles.push({ name: `${prefix}.json`, content: JSON.stringify(items, null, 2) });
            files.push(...lessonFiles);
            successLessons++;
            Object.assign(result, { status: "exported", subtitle_count: items.length });
          } else {
            noSubtitles++;
            Object.assign(result, { status: "no_subtitle", reason });
          }
        } catch (err) {
          failed++;
          Object.assign(result, { status: "error", reason: humanizeError(err) });
        }
        results.push(result);
        lessonResults.set(sid, result);
        updateCourseSummary();
        const resultEl = Array.from(listEl.querySelectorAll(".lesson-result")).find((el) => el.dataset.subId === sid);
        resultEl.textContent = result.status === "exported" ? "已导出" : result.status === "no_subtitle" ? "暂无字幕" : "请求失败";
        resultEl.title = result.reason || `${result.subtitle_count} 条字幕`;

        const pct = Math.round(((i + 1) / selectedSubIds.length) * 100);
        progressFill.style.width = `${pct}%`;
        showStatus("status-batch", `正在获取 ${i + 1}/${selectedSubIds.length}...`, "loading");
      }

      lastReport = {
        course_id: courseId, course_title: courseTitle, exported_at: new Date().toISOString(),
        excluded, results,
      };
      reportBtn.classList.remove("hidden");
      const missing = results.filter((result) => result.status !== "exported");
      document.getElementById("batch-details-text").textContent = missing.map((result) =>
        `${result.title} (${result.sub_id})：${result.status === "no_subtitle" ? "暂无字幕" : "请求失败"} — ${result.reason}`
      ).join("\n");
      if (missing.length) details.classList.remove("hidden");

      if (files.length > 0) {
        files.push({ name: "字幕导出结果.json", content: JSON.stringify(lastReport, null, 2) });
        showStatus("status-batch", "正在打包 ZIP...", "loading");
        await downloadZip(files, sanitizeFilename(`${courseTitle || `subtitles_${courseId}`}.zip`));
        showStatus("status-batch", `完成：${successLessons} 次回放已导出，${noSubtitles} 次暂无字幕，${failed} 次请求失败`,
          missing.length ? "warning" : "success");
      } else {
        showStatus("status-batch", `没有可导出的字幕：${noSubtitles} 次暂无字幕，${failed} 次请求失败。可下载结果报告查看原因。`, "warning");
      }
    } catch (err) {
      showStatus("status-batch", `导出失败：${err.message}`, "error");
    } finally {
      btn.disabled = false;
      controls.forEach((control) => (control.disabled = false));
    }
  });
});
