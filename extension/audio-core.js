(function (root, factory) {
  const core = typeof module === "object" && module.exports ? require("./core.js") : root.ScutSubtitleCore;
  const api = factory(core);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ScutAudioCore = api;
})(globalThis, function (core) {
  "use strict";
  const { SUPPORTED_HOSTS, createApi, normalizeNumericId, sanitizeFilename } = core;

  function parseTask(search) {
    const params = new URLSearchParams(search);
    for (const key of ["origin", "course_id", "sub_ids", "tab_id"]) {
      if (params.getAll(key).length !== 1) throw new Error(`音频任务缺少或重复参数：${key}`);
    }
    const origin = params.get("origin");
    createApi(origin);
    const courseId = normalizeNumericId(params.get("course_id"), "course_id");
    const subIds = [...new Set(params.get("sub_ids").split(",").map((id) => normalizeNumericId(id, "sub_id")))];
    if (subIds.length > 500) throw new Error("每次最多下载 500 次回放");
    const tabId = Number(params.get("tab_id"));
    if (!Number.isSafeInteger(tabId) || tabId < 0) throw new Error("课程标签页参数无效");
    return { origin, courseId, subIds, tabId };
  }

  function mediaFromInfo(data, origin) {
    createApi(origin);
    const content = data?.content;
    const candidates = [
      ["content.save_playback.contents", content?.save_playback?.contents],
      ["content.playback.url", content?.playback?.url],
    ];
    const videos = Object.values(data?.video_list || {}).filter((video) => video && typeof video === "object");
    videos.sort((a, b) => Number(String(a.type) !== "3") - Number(String(b.type) !== "3"));
    for (const video of videos) candidates.push([`video_list(type=${video.type})`, video.preview_url]);
    for (const [key, value] of Object.entries(data?.playurl || {})) candidates.push([`playurl.${key}`, value]);
    for (const [field, value] of candidates) {
      if (typeof value !== "string") continue;
      let url;
      try { url = new URL(value); } catch { continue; }
      if (url.protocol !== "https:" || !SUPPORTED_HOSTS.has(url.hostname) || url.port || url.username || url.password) continue;
      if (String(data.is_m3u8).toLowerCase() === "yes" || url.pathname.toLowerCase().endsWith(".m3u8")) {
        throw new Error("该回放使用 HLS，当前音频下载只支持普通 MP4");
      }
      // WebVPN metadata can still contain the internal absolute media URL.
      if (origin.includes("webvpn.scut.edu.cn") && url.hostname === "video.jw.scut.edu.cn") {
        url.hostname = new URL(origin).hostname;
      }
      return { field, url: url.href };
    }
    throw new Error("未找到受支持的回放媒体地址");
  }

  function audioFilename(courseId, subId, title) {
    courseId = normalizeNumericId(courseId, "course_id");
    subId = normalizeNumericId(subId, "sub_id");
    const date = typeof title === "string" && title.match(/^\d{4}-\d{2}-\d{2}/);
    const label = date ? date[0] : title ? sanitizeFilename(title).slice(0, 48) : "";
    return `audio_${courseId}/${label ? `${label}_` : ""}${subId}.m4a`;
  }

  return { parseTask, mediaFromInfo, audioFilename };
});
