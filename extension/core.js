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
  const NUMERIC_ID = /^\d+$/;

  function normalizeNumericId(value, fieldName) {
    if (typeof value !== "string" && typeof value !== "number") {
      throw new TypeError(`${fieldName} 必须是纯数字`);
    }
    const normalized = String(value);
    if (!NUMERIC_ID.test(normalized)) {
      throw new TypeError(`${fieldName} 必须是纯数字`);
    }
    return normalized;
  }

  function queryId(searchParams, fieldName, { required, defaultValue = null }) {
    const values = searchParams.getAll(fieldName);
    if (values.length === 0) {
      if (required) throw new TypeError(`缺少 ${fieldName}`);
      return defaultValue;
    }
    if (values.length !== 1) throw new TypeError(`${fieldName} 不能重复`);
    return normalizeNumericId(values[0], fieldName);
  }

  function parseCourseUrl(url) {
    try {
      const parsed = new URL(url);
      if (parsed.protocol !== "https:" || !SUPPORTED_HOSTS.has(parsed.hostname)) return null;
      if (parsed.username || parsed.password || parsed.port) return null;
      if (!["/livingroom", "/livingroom/"].includes(parsed.pathname)) return null;
      const subId = queryId(parsed.searchParams, "sub_id", { required: true });
      const courseId = queryId(parsed.searchParams, "course_id", { required: false });
      const tenantCode = queryId(
        parsed.searchParams,
        "tenant_code",
        { required: false, defaultValue: "21" }
      );
      return {
        origin: parsed.origin,
        hostname: parsed.hostname,
        siteKey: parsed.hostname === "video.jw.scut.edu.cn" ? "internal" : "external",
        courseId,
        subId,
        tenantCode,
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
    if (![...SUPPORTED_HOSTS].some((hostname) => origin === `https://${hostname}`)) {
      throw new TypeError("不支持的华园视频站点 origin");
    }
    return {
      subtitle: (subId) => apiUrl(
        origin,
        "/courseapi/v3/web-socket/search-trans-result",
        { sub_id: normalizeNumericId(subId, "sub_id"), format: "json" }
      ),
      subInfo: (courseId, subId) => apiUrl(
        origin,
        "/courseapi/v3/portal-home-setting/get-sub-info",
        {
          course_id: normalizeNumericId(courseId, "course_id"),
          sub_id: normalizeNumericId(subId, "sub_id"),
        }
      ),
      courseTitle: (courseId) => apiUrl(
        origin,
        "/courseapi/v3/multi-search/get-course-teacher-others",
        { course_id: normalizeNumericId(courseId, "course_id"), per_page: "1" }
      ),
      catalogue: (courseId) => apiUrl(
        origin,
        "/courseapi/v2/course/catalogue",
        { course_id: normalizeNumericId(courseId, "course_id") }
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

  function subtitleText(item) {
    if (!item || typeof item !== "object") return null;
    const text = typeof item.Text === "string" ? item.Text.trim() : "";
    return text || null;
  }

  function finiteNumber(value) {
    if (
      (typeof value !== "number" && typeof value !== "string") ||
      (typeof value === "string" && !value.trim())
    ) {
      return null;
    }
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function validTimedSubtitle(item) {
    const text = subtitleText(item);
    if (!text || !Object.hasOwn(item, "BeginSec")) return null;
    const begin = finiteNumber(item.BeginSec);
    if (begin === null || begin < 0) return null;
    const rawEnd = item.EndSec;
    const end = rawEnd === undefined || rawEnd === null ? begin + 5 : finiteNumber(rawEnd);
    if (end === null || end < begin) {
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
      .map(validTimedSubtitle)
      .filter(Boolean)
      .map((item, index) =>
        `${index + 1}\n${formatTime(item.begin)} --> ${formatTime(item.end)}\n${item.text}`
      )
      .join("\n\n");
  }

  function toTxt(items) {
    return items
      .map(subtitleText)
      .filter(Boolean)
      .join("\n");
  }

  function sanitizeFilename(name) {
    const cleaned = String(name)
      .replace(/[\u0000-\u001f\u007f\/\\:*?"<>|]/g, "_")
      .replace(/\s+/g, " ")
      .replace(/^[. ]+|[. ]+$/g, "")
      .trim();
    return cleaned || "untitled";
  }

  function sanitizeIdForFilename(value, fieldName = "ID") {
    return sanitizeFilename(normalizeNumericId(value, fieldName));
  }

  return {
    SUPPORTED_HOSTS,
    normalizeNumericId,
    parseCourseUrl,
    createApi,
    extractSubtitleItems,
    formatTime,
    toSrt,
    toTxt,
    sanitizeFilename,
    sanitizeIdForFilename,
  };
});
