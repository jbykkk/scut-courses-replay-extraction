const API_BASE = "https://video.jw.scut.edu.cn";
const API = {
  SUBTITLE: (subId) => `${API_BASE}/courseapi/v3/web-socket/search-trans-result?sub_id=${encodeURIComponent(subId)}&format=json`,
  SUB_INFO: (courseId, subId) => `${API_BASE}/courseapi/v3/portal-home-setting/get-sub-info?course_id=${encodeURIComponent(courseId)}&sub_id=${encodeURIComponent(subId)}`,
  COURSE_TITLE: (courseId) => `${API_BASE}/courseapi/v3/multi-search/get-course-teacher-others?course_id=${encodeURIComponent(courseId)}&per_page=1`,
  CATALOGUE: (courseId) => `${API_BASE}/courseapi/v2/course/catalogue?course_id=${encodeURIComponent(courseId)}`,
};

// === URL parsing ===

function parseCourseUrl(url) {
  try {
    const u = new URL(url);
    if (u.hostname !== "video.jw.scut.edu.cn") return null;
    const subId = u.searchParams.get("sub_id");
    if (!subId) return null;
    return { subId, courseId: u.searchParams.get("course_id") };
  } catch {
    return null;
  }
}

// === Filename sanitization ===

function sanitizeFilename(name) {
  const cleaned = name.replace(/[\/\\:*?"<>|]/g, "_").replace(/\s+/g, " ").trim();
  return cleaned || "untitled";
}

// === API calls ===

async function fetchJson(url) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const resp = await fetch(url, { credentials: "include", signal: controller.signal });
      if (!resp.ok) {
        const error = new Error(`HTTP ${resp.status}`);
        error.retryable = [408, 429].includes(resp.status) || resp.status >= 500;
        throw error;
      }
      return await resp.json();
    } catch (err) {
      if (attempt === 0 && (err.retryable || err.name === "TypeError" || err.name === "AbortError")) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        continue;
      }
      if (err.name === "AbortError") throw new Error("请求超时，请重试");
      if (err.name === "SyntaxError") throw new Error("接口未返回 JSON，请检查登录状态");
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }
}

function checkApiResult(body) {
  for (const result of [body?.result, body]) {
    if (!result || typeof result !== "object") continue;
    if (result.success === false || (result.code != null && !["0", "200"].includes(String(result.code)))) {
      throw new Error(result.msg || result.message || (result.code != null ? `接口错误 ${result.code}` : "接口返回失败"));
    }
  }
}

async function fetchSubtitle(subId) {
  const body = await fetchJson(API.SUBTITLE(subId));
  // 10002 是平台明确返回的“未查询到语音数据”，区别于登录或请求失败。
  if (String(body?.code) !== "10002") checkApiResult(body);
  return body;
}

async function loadSubtitle(subId) {
  const body = await fetchSubtitle(subId);
  const items = extractSubtitleItems(body);
  return {
    items,
    reason: items.length ? "" : (String(body?.code) === "10002" ? body.msg : "平台暂无字幕数据，可能尚未生成"),
  };
}

async function fetchCourseInfo(courseId, subId) {
  const body = await fetchJson(API.SUB_INFO(courseId, subId));
  checkApiResult(body);
  return body?.data || null;
}

async function fetchCourseTitle(courseId) {
  const body = await fetchJson(API.COURSE_TITLE(courseId));
  checkApiResult(body);
  return body?.data?.[0]?.course_title || null;
}

async function fetchCatalogue(courseId) {
  const body = await fetchJson(API.CATALOGUE(courseId));
  checkApiResult(body);
  const lessons = body?.result?.data;
  if (!Array.isArray(lessons)) throw new Error("课程目录格式异常，请重试");
  return lessons;
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

// === JSON parsing ===

function extractSubtitleItems(body) {
  if (Array.isArray(body)) {
    if (body.length > 0 && body[0] && typeof body[0] === "object" && "BeginSec" in body[0]) return body;
    for (const item of body) {
      if (item && typeof item === "object") {
        const inner = extractSubtitleItems(item);
        if (inner.length > 0) return inner;
      }
    }
    return [];
  }
  if (body && typeof body === "object") {
    for (const key of ["all_content", "data", "result", "list", "rows"]) {
      const value = body[key];
      if (value && typeof value === "object") {
        const inner = extractSubtitleItems(value);
        if (inner.length > 0) return inner;
      }
    }
  }
  return [];
}

// === Format conversion ===

function formatTime(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const ms = Math.floor((seconds - Math.floor(seconds)) * 1000);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")},${String(ms).padStart(3, "0")}`;
}

function toSrt(items) {
  let idx = 0;
  return items
    .filter((item) => item.Text?.trim())
    .map((item) => {
      idx++;
      const start = formatTime(item.BeginSec);
      const end = formatTime(item.EndSec || item.BeginSec + 5);
      return `${idx}\n${start} --> ${end}\n${item.Text.trim()}`;
    })
    .join("\n\n");
}

function toTxt(items) {
  return items
    .filter((item) => item.Text?.trim())
    .map((item) => item.Text.trim())
    .join("\n");
}

// === Download ===

function downloadFile(content, filename) {
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  return downloadBlob(blob, filename);
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  return new Promise((resolve, reject) => {
    chrome.downloads.download({ url, filename, saveAs: false }, (downloadId) => {
      const error = chrome.runtime.lastError;
      URL.revokeObjectURL(url);
      if (error) reject(new Error(error.message));
      else resolve(downloadId);
    });
  });
}

async function downloadZip(files, zipName) {
  const zip = new JSZip();
  for (const { name, content } of files) {
    zip.file(name, content);
  }
  const blob = await zip.generateAsync({ type: "blob" });
  await downloadBlob(blob, zipName);
}

// === UI helpers ===

function showStatus(id, text, type) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = `status ${type}`;
}

// === Main ===

document.addEventListener("DOMContentLoaded", async () => {
  const mainEl = document.getElementById("main");
  const errorEl = document.getElementById("error-page");

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const parsed = parseCourseUrl(tab?.url);

  if (!parsed) {
    errorEl.classList.remove("hidden");
    return;
  }

  const { subId, courseId } = parsed;
  mainEl.classList.remove("hidden");

  let courseTitle = null;
  let catalogue = [];
  let lastReport = null;
  const lessonResults = new Map();

  // === Load course info (single tab) ===
  if (courseId) {
    const [info, title] = await Promise.all([
      fetchCourseInfo(courseId, subId).catch(() => null),
      fetchCourseTitle(courseId).catch(() => null),
    ]);
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
      const { items, reason } = await loadSubtitle(subId);
      if (items.length === 0) {
        showStatus("status-single", `该课时暂无字幕：${reason}`, "warning");
        return;
      }
      if (wantSrt) await downloadFile(toSrt(items), `subtitle_${subId}.srt`);
      if (wantTxt) await downloadFile(toTxt(items), `subtitle_${subId}.txt`);
      if (wantJson) await downloadFile(JSON.stringify(items, null, 2), `subtitle_${subId}.json`);
      showStatus("status-single", `已下载 ${items.length} 条字幕`, "success");
    } catch (err) {
      showStatus("status-single", err.message || "获取失败", "error");
    } finally {
      btn.disabled = false;
    }
  });

  // === Batch: load catalogue ===
  let catalogueError = null;
  const excluded = { upcoming: 0, live: 0, unavailable: 0 };
  if (courseId) {
    try {
      const lessons = await fetchCatalogue(courseId);
      const now = Date.now();
      const seen = new Set();
      catalogue = lessons.filter((lesson) => {
        const sid = String(lesson.sub_id ?? "");
        if (!sid || seen.has(sid)) return false;
        seen.add(sid);
        const state = classifyLesson(lesson, now);
        if (state === "replay") return true;
        excluded[state]++;
        return false;
      }).map((lesson) => ({ ...lesson, sub_id: String(lesson.sub_id) }));
    } catch (err) {
      catalogueError = err.message;
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
        const prefix = `${String(i + 1).padStart(2, "0")}_${label}_${sid}`;
        const result = { sub_id: sid, title: lesson?.title || sid };

        try {
          const { items, reason } = await loadSubtitle(sid);
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
          Object.assign(result, { status: "error", reason: err.message || "请求失败" });
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
