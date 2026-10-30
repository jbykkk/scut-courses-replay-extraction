export function saveBlob(blob, filename, signal) {
  signal?.throwIfAborted();
  const url = URL.createObjectURL(blob);
  return new Promise((resolve, reject) => {
    let downloadId;
    let settled = false;
    let timer;
    function finish(error) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      chrome.downloads.onChanged.removeListener(changed);
      signal?.removeEventListener("abort", cancel);
      URL.revokeObjectURL(url);
      if (error) reject(error);
      else resolve(downloadId);
    }
    function cancel() {
      if (downloadId !== undefined) chrome.downloads.cancel(downloadId, () => void chrome.runtime.lastError);
      finish(signal.reason || new DOMException("下载已取消", "AbortError"));
    }
    function changed(delta) {
      if (delta.id !== downloadId) return;
      if (delta.state?.current === "complete") finish();
      else if (delta.state?.current === "interrupted") finish(new Error(`文件保存失败：${delta.error?.current || "下载被中断"}`));
    }
    chrome.downloads.onChanged.addListener(changed);
    signal?.addEventListener("abort", cancel, { once: true });
    try {
      chrome.downloads.download({ url, filename, saveAs: false, conflictAction: "uniquify" }, (id) => {
      const error = chrome.runtime.lastError;
      downloadId = id;
      if (settled) {
        if (id !== undefined) chrome.downloads.cancel(id, () => void chrome.runtime.lastError);
        return;
      }
      if (error || id === undefined) {
        finish(new Error(`浏览器未能创建下载任务：${error?.message || "未知错误"}`));
        return;
      }
      timer = setTimeout(() => finish(new Error("文件保存超时，请检查浏览器下载列表")), 120000);
      // Catch completions that occurred before the download callback.
      chrome.downloads.search({ id }, (items) => {
        if (chrome.runtime.lastError) {
          finish(new Error(chrome.runtime.lastError.message));
          return;
        }
        const item = items[0];
        if (item) changed({ id, state: { current: item.state }, error: { current: item.error } });
      });
      });
    } catch (error) {
      finish(error);
    }
  });
}
