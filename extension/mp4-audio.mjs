import { createFile, Log } from "./vendor/mp4box.all.mjs";
import { inspectAudio, downloadIndexedAudio, MediaRequestError, MediaLimitError } from "./mp4-audio-range.mjs";

Log.setLogLevel(Log.error);
const MAX_BYTES = 1024 * 1024 * 1024;

export function createAudioExtractor() {
  // Keeping mdat also supports recordings with the moov metadata at the end.
  const file = createFile(true);
  const parts = [];
  let track = null;
  let samples = 0;
  let failure = null;
  file.onError = (message) => { failure = new Error(`MP4 解析失败：${message}`); };
  file.onSegment = (id, user, buffer, sampleNumber) => {
    parts.push(buffer);
    samples = sampleNumber;
    file.releaseUsedSamples(id, sampleNumber);
  };
  file.onReady = (info) => {
    track = info.audioTracks.find((item) => item.codec.startsWith("mp4a.40."));
    if (!track || !track.nb_samples) {
      failure = new Error("回放中没有可提取的 AAC 音轨");
      return;
    }
    // Use the M4A brand for the exported audio container.
    file.ftyp.major_brand = "M4A ";
    file.ftyp.compatible_brands = ["M4A ", "isom", "iso6"];
    file.setSegmentOptions(track.id, null, { nbSamples: 1000, rapAlignement: false });
    parts.push(file.initializeSegmentation().buffer);
    file.start();
  };
  return {
    append(bytes, position) {
      const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
      buffer.fileStart = position;
      file.appendBuffer(buffer);
      if (failure) throw failure;
    },
    finish() {
      file.flush();
      if (failure) throw failure;
      if (!track) throw new Error("未读取到完整 MP4 元信息，请确认回放可播放");
      if (samples !== track.nb_samples) throw new Error("回放音轨不完整，请重试下载");
      // Chrome changes the file extension to .mp4 for the generic audio/mp4 MIME.
      const blob = new Blob(parts, { type: "audio/x-m4a" });
      return { blob, duration: track.duration / track.timescale, codec: track.codec };
    },
    dispose() {
      file.stop();
      parts.length = 0;
    },
  };
}

async function extractFullAudio(url, { signal, onProgress = () => {} } = {}) {
  const controller = new AbortController();
  const cancel = () => controller.abort(signal?.reason);
  signal?.throwIfAborted();
  signal?.addEventListener("abort", cancel, { once: true });
  let timer;
  let reader;
  const extractor = createAudioExtractor();
  const resetTimeout = () => {
    clearTimeout(timer);
    timer = setTimeout(() => controller.abort(new Error("媒体读取超时，请重试")), 30000);
  };
  try {
    resetTimeout();
    const response = await fetch(url, { credentials: "include", signal: controller.signal });
    if (!response.ok) throw new Error(`回放媒体请求失败（HTTP ${response.status}）`);
    const total = Number(response.headers.get("content-length")) || 0;
    if (total > MAX_BYTES) throw new Error("回放文件超过 1 GB，请使用命令行 audio 下载");
    if (!response.body) throw new Error("浏览器没有返回媒体内容");
    reader = response.body.getReader();
    let received = 0;
    while (true) {
      resetTimeout();
      const { value, done } = await reader.read();
      if (done) break;
      signal?.throwIfAborted();
      if (received + value.byteLength > MAX_BYTES) throw new Error("回放文件超过 1 GB，请使用命令行 audio 下载");
      extractor.append(value, received);
      received += value.byteLength;
      onProgress(received, total);
    }
    signal?.throwIfAborted();
    if (total && received !== total) throw new Error("媒体下载不完整，请重试");
    return { ...extractor.finish(), method: "full", transferredBytes: received, mediaBytes: total || received, requests: 1 };
  } catch (error) {
    if (controller.signal.aborted) throw controller.signal.reason;
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
    controller.abort();
    await reader?.cancel().catch(() => {});
    extractor.dispose();
  }
}

export async function extractAudio(url, { signal, onProgress = () => {} } = {}) {
  signal?.throwIfAborted();
  const controller = new AbortController();
  const cancel = () => controller.abort(signal.reason);
  signal?.addEventListener("abort", cancel, { once: true });
  const stats = { requests: 0, transferred_bytes: 0 };
  let index;
  let phase = "index";
  let fraction = 0;
  let fallbackReason;
  const notify = () => onProgress(stats.transferred_bytes, index?.reader.total || 0, {
    mode: "ranges", phase, progress: phase === "index" ? null : fraction,
  });
  try {
    notify();
    index = await inspectAudio(url, { signal: controller.signal, stats, maxMediaBytes: MAX_BYTES, onBytes: notify });
    phase = "audio";
    const { blob, summary } = await downloadIndexedAudio(index, {
      signal: controller.signal,
      onProgress(value) { fraction = value; notify(); },
    });
    signal?.throwIfAborted();
    return {
      blob, duration: index.track.duration / index.track.timescale, codec: index.track.codec,
      method: "ranges", transferredBytes: summary.transferred_bytes, mediaBytes: summary.media_bytes,
      requests: summary.requests,
    };
  } catch (error) {
    signal?.throwIfAborted();
    if (error instanceof MediaRequestError || error instanceof MediaLimitError) throw error;
    fallbackReason = error.message;
  } finally {
    controller.abort();
    signal?.removeEventListener("abort", cancel);
    index?.file.stop();
  }
  signal?.throwIfAborted();
  onProgress(stats.transferred_bytes, 0, { mode: "full", phase: "fallback" });
  const result = await extractFullAudio(url, {
    signal,
    onProgress(received, total) {
      onProgress(received + stats.transferred_bytes, total, { mode: "full", progress: total ? received / total : null });
    },
  });
  return { ...result, transferredBytes: result.transferredBytes + stats.transferred_bytes,
    requests: result.requests + stats.requests, fallbackReason };
}
