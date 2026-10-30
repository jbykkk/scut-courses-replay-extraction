import { extractAudio } from "./mp4-audio.mjs";
import { saveBlob } from "./audio-download.mjs";

const { createApi } = ScutSubtitleCore;
const { parseTask, mediaFromInfo, audioFilename } = ScutAudioCore;
const { requestJson, checkApiResult, humanizeError } = ScutRequests;
const summary = document.getElementById("audio-summary");
const progress = document.getElementById("audio-progress");
const cancelButton = document.getElementById("cancel-audio");
const retryButton = document.getElementById("retry-audio");
const reportButton = document.getElementById("audio-report-btn");
const results = new Map();
const rows = new Map();
let controller = null;
let task;

function setRow(id, state, message, title) {
  const row = rows.get(id);
  row.dataset.state = state;
  if (title) row.querySelector(".lesson-label").textContent = title;
  row.querySelector(".audio-state").textContent = message;
}

function unfinished() {
  return task.subIds.filter((id) => results.get(id)?.status !== "downloaded");
}

async function run(ids) {
  if (controller) return;
  controller = new AbortController();
  const { signal } = controller;
  cancelButton.disabled = false;
  retryButton.disabled = true;
  reportButton.disabled = true;
  summary.className = "status";
  const api = createApi(task.origin);
  try {
    for (const [index, id] of ids.entries()) {
      if (signal.aborted) break;
      summary.textContent = `正在处理 ${index + 1}/${ids.length} 次回放…`;
      progress.value = 0;
      setRow(id, "loading", "正在获取回放信息…");
      const result = { sub_id: id };
      try {
        const body = await requestJson(api.subInfo(task.courseId, id), { tabId: task.tabId, origin: task.origin });
        signal.throwIfAborted();
        checkApiResult(body);
        const data = body.data;
        const media = mediaFromInfo(data, task.origin);
        result.title = data.sub_title || id;
        result.media_url = media.url;
        result.source_field = media.field;
        setRow(id, "loading", "正在读取回放并提取音轨…", result.title);
        const { blob, duration, codec } = await extractAudio(media.url, {
          signal,
          onProgress(received, total) {
            const amount = (received / 1024 / 1024).toFixed(1);
            if (total) progress.value = Math.round(received * 100 / total);
            else progress.removeAttribute("value");
            setRow(id, "loading", `已读取 ${amount} MB${total ? ` / ${(total / 1024 / 1024).toFixed(1)} MB` : ""}，正在提取音轨…`);
          },
        });
        signal.throwIfAborted();
        const filename = audioFilename(task.courseId, id, result.title);
        setRow(id, "saving", "音轨提取完成，正在保存文件…");
        const downloadId = await saveBlob(blob, filename, signal);
        Object.assign(result, {
          status: "downloaded", audio_file: filename, download_id: downloadId,
          duration_seconds: Math.round(duration * 1000) / 1000,
          size_bytes: blob.size, codec, downloaded_at: new Date().toISOString(),
        });
        setRow(id, "downloaded", `已保存 · ${(duration / 60).toFixed(1)} 分钟 · ${(blob.size / 1024 / 1024).toFixed(1)} MB`);
      } catch (error) {
        result.status = signal.aborted ? "cancelled" : "error";
        result.reason = signal.aborted ? "已取消" : humanizeError(error);
        setRow(id, result.status, result.reason);
      }
      results.set(id, result);
    }
  } finally {
    for (const id of ids) {
      if (!results.has(id) || (signal.aborted && rows.get(id).dataset.state === "loading")) {
        results.set(id, { sub_id: id, status: "cancelled", reason: "已取消" });
        setRow(id, "cancelled", "已取消");
      }
    }
    const completed = [...results.values()].filter((result) => result.status === "downloaded").length;
    const failed = [...results.values()].filter((result) => result.status === "error").length;
    summary.textContent = `${signal.aborted ? "已取消" : "完成"}：${completed} 次音频已保存，${failed} 次失败，${task.subIds.length - completed - failed} 次未完成`;
    summary.className = `status ${completed === task.subIds.length ? "success" : "warning"}`;
    progress.value = 100;
    cancelButton.disabled = true;
    retryButton.disabled = unfinished().length === 0;
    reportButton.disabled = false;
    controller = null;
  }
}

cancelButton.addEventListener("click", () => controller?.abort());
retryButton.addEventListener("click", () => run(unfinished()));
reportButton.addEventListener("click", async () => {
  reportButton.disabled = true;
  const reportStatus = document.getElementById("report-status");
  try {
    const entries = task.subIds.map((id) => results.get(id));
    const report = { course_id: task.courseId, origin: task.origin, exported_at: new Date().toISOString(),
      items: entries.filter((item) => item.status === "downloaded"), results: entries };
    await saveBlob(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }), `audio_${task.courseId}/媒体地址.json`);
    reportStatus.textContent = "音频结果报告已保存";
  } catch (error) {
    reportStatus.textContent = `报告保存失败：${humanizeError(error)}`;
  } finally {
    reportButton.disabled = false;
  }
});

try {
  task = parseTask(location.search);
  for (const id of task.subIds) {
    const row = document.createElement("li");
    const label = document.createElement("span");
    label.className = "lesson-label";
    label.textContent = `课时 ${id}`;
    const state = document.createElement("span");
    state.className = "audio-state";
    state.textContent = "等待下载";
    row.append(label, state);
    document.getElementById("audio-results").append(row);
    rows.set(id, row);
  }
  await run(task.subIds);
} catch (error) {
  summary.className = "status error";
  summary.textContent = humanizeError(error);
}
