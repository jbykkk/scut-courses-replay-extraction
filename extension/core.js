(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ScutSubtitleCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const SUPPORTED_HOSTS = new Set([
    "video.jw.scut.edu.cn",
    "video-jw-443.webvpn.scut.edu.cn",
  ]);

  function parseCourseUrl(url) {
    try {
      const parsed = new URL(url);
      if (parsed.protocol !== "https:" || !SUPPORTED_HOSTS.has(parsed.hostname)) return null;
      if (parsed.pathname.replace(/\/+$/, "") !== "/livingroom") return null;
      const subId = parsed.searchParams.get("sub_id");
      if (!subId) return null;
      return {
        origin: parsed.origin,
        hostname: parsed.hostname,
        siteKey: parsed.hostname === "video.jw.scut.edu.cn" ? "internal" : "external",
        courseId: parsed.searchParams.get("course_id"),
        subId,
        tenantCode: parsed.searchParams.get("tenant_code") || "21",
      };
    } catch {
      return null;
    }
  }

  function apiUrl(origin, path, params) {
    const url = new URL(path, origin);
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== "") {
        url.searchParams.set(key, value);
      }
    }
    return url.href;
  }

  function createApi(origin) {
    return {
      subtitle: (subId) => apiUrl(
        origin,
        "/courseapi/v3/web-socket/search-trans-result",
        { sub_id: subId, format: "json" }
      ),
      subInfo: (courseId, subId) => apiUrl(
        origin,
        "/courseapi/v3/portal-home-setting/get-sub-info",
        { course_id: courseId, sub_id: subId }
      ),
      courseTitle: (courseId) => apiUrl(
        origin,
        "/courseapi/v3/multi-search/get-course-teacher-others",
        { course_id: courseId, per_page: "1" }
      ),
      catalogue: (courseId) => apiUrl(
        origin,
        "/courseapi/v2/course/catalogue",
        { course_id: courseId }
      ),
    };
  }

  function extractSubtitleItems(body) {
    if (Array.isArray(body)) {
      if (
        body.length > 0 &&
        body.every((item) => item && typeof item === "object" && !Array.isArray(item)) &&
        body.some((item) => "BeginSec" in item && "Text" in item)
      ) {
        return body;
      }
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

  function validSubtitle(item) {
    if (!item || typeof item !== "object") return null;
    const text = typeof item.Text === "string" ? item.Text.trim() : "";
    const begin = Number(item.BeginSec);
    const rawEnd = item.EndSec;
    const end = rawEnd === undefined || rawEnd === null ? begin + 5 : Number(rawEnd);
    if (!text || !Number.isFinite(begin) || !Number.isFinite(end) || begin < 0 || end < begin) {
      return null;
    }
    return { text, begin, end };
  }

  function formatTime(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    const ms = Math.floor((seconds - Math.floor(seconds)) * 1000);
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")},${String(ms).padStart(3, "0")}`;
  }

  function toSrt(items) {
    return items
      .map(validSubtitle)
      .filter(Boolean)
      .map((item, index) =>
        `${index + 1}\n${formatTime(item.begin)} --> ${formatTime(item.end)}\n${item.text}`
      )
      .join("\n\n");
  }

  function toTxt(items) {
    return items
      .map(validSubtitle)
      .filter(Boolean)
      .map((item) => item.text)
      .join("\n");
  }

  function sanitizeFilename(name) {
    const cleaned = String(name).replace(/[\/\\:*?"<>|]/g, "_").replace(/\s+/g, " ").trim();
    return cleaned || "untitled";
  }

  return {
    SUPPORTED_HOSTS,
    parseCourseUrl,
    createApi,
    extractSubtitleItems,
    formatTime,
    toSrt,
    toTxt,
    sanitizeFilename,
  };
});
