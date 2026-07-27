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
  let response;
  try {
    response = await fetch(url, { credentials: "include" });
  } catch (error) {
    throw new RequestError(error.message || "网络请求失败", { kind: "network" });
  }
  if (!response.ok) {
    const kind = response.status === 401 || response.status === 403 ? "auth" : "http";
    throw new RequestError(`HTTP ${response.status}`, { status: response.status, kind });
  }
  console.info(`[SCUT] 接口响应：${url} status=${response.status}`);
  const contentType = response.headers.get("content-type") || "";
  try {
    return JSON.parse(await response.text());
  } catch {
    throw new RequestError(
      `接口正文不是 JSON，可能需要重新登录（Content-Type: ${contentType || "未知"}）`,
      { kind: "auth" }
    );
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
          const response = await fetch(target.href, { credentials: "include" });
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
          return { ok: false, kind: "network", message: error.message || "页面内请求失败" };
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

  async function safeRequest(url) {
    try {
      return await requestJson(url, pageContext);
    } catch (error) {
      console.warn(`[SCUT] 可选信息获取失败：${url}`, error);
      return null;
    }
  }

  if (courseId) {
    const [infoBody, titleBody] = await Promise.all([
      safeRequest(api.subInfo(courseId, subId)),
      safeRequest(api.courseTitle(courseId)),
    ]);
    const info = infoBody?.data || null;
    courseTitle = titleBody?.data?.[0]?.course_title || null;
    if (courseTitle) document.getElementById("course-name").textContent = courseTitle;
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

  document.querySelectorAll(".tab").forEach((tabButton) => {
    tabButton.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
      document.querySelectorAll(".tab-content").forEach((item) => item.classList.remove("active"));
      tabButton.classList.add("active");
      document.getElementById(`tab-${tabButton.dataset.tab}`).classList.add("active");
    });
  });

  document.getElementById("download-btn").addEventListener("click", async () => {
    const wantSrt = document.getElementById("fmt-srt").checked;
    const wantTxt = document.getElementById("fmt-txt").checked;
    const wantJson = document.getElementById("fmt-json").checked;
    if (!wantSrt && !wantTxt && !wantJson) {
      showStatus("status-single", "请至少选择一种格式", "error");
      return;
    }
    const button = document.getElementById("download-btn");
    button.disabled = true;
    showStatus("status-single", "正在获取字幕...", "loading");
    try {
      const items = extractSubtitleItems(await requestJson(api.subtitle(subId), pageContext));
      if (items.length === 0) {
        showStatus("status-single", "该课时暂无字幕数据", "error");
        return;
      }
      const downloads = [];
      if (wantSrt) downloads.push(downloadFile(toSrt(items), `subtitle_${subId}.srt`));
      if (wantTxt) downloads.push(downloadFile(toTxt(items), `subtitle_${subId}.txt`));
      if (wantJson) {
        downloads.push(
          downloadFile(JSON.stringify(items, null, 2), `subtitle_${subId}.json`)
        );
      }
      await Promise.all(downloads);
      showStatus("status-single", `已下载 ${items.length} 条字幕`, "success");
    } catch (error) {
      showStatus("status-single", humanizeError(error), "error");
    } finally {
      button.disabled = false;
    }
  });

  if (courseId) {
    try {
      const body = await requestJson(api.catalogue(courseId), pageContext);
      catalogue = (body?.result?.data || []).filter((lesson) => {
        try {
          lesson.sub_id = ScutSubtitleCore.normalizeNumericId(lesson.sub_id, "sub_id");
          return true;
        } catch {
          console.warn("[SCUT] 跳过包含非法 sub_id 的课程目录项", lesson);
          return false;
        }
      });
    } catch (error) {
      console.warn("[SCUT] 课程目录获取失败", error);
      showStatus("status-batch", `课程目录获取失败：${humanizeError(error)}`, "error");
    }
  }

  const listEl = document.getElementById("lesson-list");
  const countEl = document.getElementById("lesson-count");
  if (catalogue.length === 0) {
    countEl.textContent = courseId ? "未找到课程目录" : "链接缺少 course_id";
  } else {
    countEl.textContent = `已选 ${catalogue.length} / ${catalogue.length} 节`;
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
      item.append(checkbox, label);
      listEl.appendChild(item);
    });
    const selectAll = document.getElementById("select-all");
    selectAll.addEventListener("change", () => {
      listEl.querySelectorAll(".lesson-check").forEach((item) => { item.checked = selectAll.checked; });
      updateCount();
    });
    listEl.addEventListener("change", (event) => {
      if (event.target.classList.contains("lesson-check")) updateCount();
    });
  }

  function getSelectedLessons() {
    return Array.from(listEl.querySelectorAll(".lesson-check:checked")).map((item) => item.dataset.subId);
  }

  function updateCount() {
    const selected = getSelectedLessons().length;
    countEl.textContent = `已选 ${selected} / ${catalogue.length} 节`;
    document.getElementById("select-all").checked = selected === catalogue.length;
  }

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

    const button = document.getElementById("batch-download-btn");
    const progressBar = document.getElementById("batch-progress-bar");
    const progressFill = document.getElementById("batch-progress-fill");
    button.disabled = true;
    progressBar.classList.remove("hidden");
    const files = [];
    const failed = [];
    let successLessons = 0;

    for (let index = 0; index < selectedSubIds.length; index++) {
      const sid = selectedSubIds[index];
      const lesson = catalogue.find((item) => String(item.sub_id) === String(sid));
      const label = sanitizeFilename(lesson?.title || sid);
      const safeSid = sanitizeIdForFilename(sid, "sub_id");
      const prefix = `${String(index + 1).padStart(2, "0")}_${label}_${safeSid}`;
      try {
        const items = extractSubtitleItems(await requestJson(api.subtitle(sid), pageContext));
        if (items.length === 0) throw new RequestError("暂无字幕数据", { kind: "empty" });
        if (wantSrt) files.push({ name: `${prefix}.srt`, content: toSrt(items) });
        if (wantTxt) files.push({ name: `${prefix}.txt`, content: toTxt(items) });
        if (wantJson) files.push({ name: `${prefix}.json`, content: JSON.stringify(items, null, 2) });
        successLessons++;
      } catch (error) {
        failed.push(label);
        console.warn(`[SCUT] 课时 "${label}" (${sid}) 获取失败：`, error);
      }
      progressFill.style.width = `${Math.round(((index + 1) / selectedSubIds.length) * 100)}%`;
      showStatus("status-batch", `正在获取 ${index + 1}/${selectedSubIds.length}...`, "loading");
    }

    if (files.length > 0) {
      await downloadZip(files, sanitizeFilename(`${courseTitle || "subtitles"}.zip`));
      const summary = `${successLessons} 节成功，${failed.length} 节失败，${files.length} 个文件`;
      showStatus(
        "status-batch",
        failed.length ? `完成：${summary}（失败：${failed.slice(0, 3).join("、")}${failed.length > 3 ? "..." : ""}）` : `完成：${summary}`,
        "success"
      );
    } else {
      showStatus("status-batch", "未能获取任何字幕数据，请检查登录状态和课程字幕", "error");
    }
    button.disabled = false;
  });
});
