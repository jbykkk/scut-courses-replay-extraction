"use strict";

globalThis.ScutRequests = (() => {
  const { SUPPORTED_HOSTS, parseCourseUrl } = ScutSubtitleCore;

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


  return { RequestError, requestJson, humanizeError, checkApiResult };
})();
