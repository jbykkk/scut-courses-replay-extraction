#!/usr/bin/env node
// Experimental, opt-in downloader. The extension still uses its existing path.
import { readFile, writeFile, mkdir, access } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { createFile, Log } from "../extension/vendor/mp4box.all.mjs";

Log.setLogLevel(Log.error);
const MIB = 1024 * 1024;

function contentRange(value) {
  const match = /^bytes (\d+)-(\d+)\/(\d+)$/i.exec(value || "");
  if (!match) throw new Error("Missing or invalid Content-Range");
  const [start, last, total] = match.slice(1).map(Number);
  if (![start, last, total].every(Number.isSafeInteger) || start > last || last >= total) {
    throw new Error("Invalid Content-Range bounds");
  }
  return { start, end: last + 1, total };
}

export function parseMultipart(buffer, boundary) {
  const bytes = Buffer.from(buffer);
  const marker = Buffer.from(`--${boundary}`);
  let position = bytes.indexOf(marker);
  if (!boundary || boundary.length > 70 || position < 0 || position > 1024) throw new Error("Invalid multipart boundary");
  const parts = [];
  while (true) {
    if (!bytes.subarray(position, position + marker.length).equals(marker)) throw new Error("Invalid multipart delimiter");
    position += marker.length;
    if (bytes.subarray(position, position + 2).toString() === "--") break;
    if (bytes.subarray(position, position + 2).toString() !== "\r\n") throw new Error("Invalid multipart headers");
    position += 2;
    const headerEnd = bytes.indexOf("\r\n\r\n", position);
    if (headerEnd < 0 || headerEnd - position > 8192) throw new Error("Invalid multipart headers");
    const headers = bytes.subarray(position, headerEnd).toString("ascii");
    const values = [...headers.matchAll(/^Content-Range:\s*([^\r\n]+)\r?$/gim)];
    if (values.length !== 1) throw new Error("Invalid multipart Content-Range");
    const range = contentRange(values[0][1].trim());
    position = headerEnd + 4;
    const data = bytes.subarray(position, position + range.end - range.start);
    if (data.length !== range.end - range.start) throw new Error("Truncated multipart payload");
    parts.push({ ...range, data });
    position += data.length;
    if (bytes.subarray(position, position + 2).toString() !== "\r\n") throw new Error("Truncated multipart payload");
    position += 2;
  }
  if (!parts.length) throw new Error("Empty multipart response");
  return parts;
}

export function createRangeReader(url, { signal, fetchImpl = fetch } = {}) {
  const stats = { requests: 0, transferred_bytes: 0 };
  let total = null;
  let validator = null;
  return {
    stats,
    get total() { return total; },
    async read(ranges) {
      signal?.throwIfAborted();
      const requested = ranges.map(({ start, end }) => ({ start, end: total ? Math.min(end, total) : end }));
      if (!requested.length || requested.some(({ start, end }) => !Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || end <= start)) {
        throw new Error("Invalid requested byte ranges");
      }
      const headers = { Range: `bytes=${requested.map(({ start, end }) => `${start}-${end - 1}`).join(",")}`, "Accept-Encoding": "identity" };
      if (validator) headers["If-Range"] = validator;
      stats.requests++;
      const response = await fetchImpl(url, {
        headers, signal: AbortSignal.any([AbortSignal.timeout(30000), ...(signal ? [signal] : [])]),
      });
      let streamReader;
      try {
        // Never consume a full video when a server ignores or rejects Range.
        if (response.status !== 206) throw new Error(`Range unsupported or resource changed (HTTP ${response.status})`);
        if (response.headers.get("Content-Encoding") && response.headers.get("Content-Encoding") !== "identity") throw new Error("Encoded range response is unsupported");
        const limit = requested.reduce((sum, r) => sum + r.end - r.start, 0) + Math.max(4096, requested.length * 1024);
        if (Number(response.headers.get("Content-Length")) > limit) throw new Error("Range response exceeds transfer budget");
        const reader = streamReader = response.body.getReader();
        const chunks = [];
        let received = 0;
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          stats.transferred_bytes += value.byteLength;
          received += value.byteLength;
          if (received > limit) throw new Error("Range response exceeds transfer budget");
          chunks.push(value);
        }
        const bytes = Buffer.concat(chunks);
        const type = response.headers.get("Content-Type") || "";
        const multipart = /^multipart\/byteranges\s*;/i.test(type);
        const boundary = /\bboundary=(?:"([^"]+)"|([^;\s]+))/i.exec(type);
        const parts = multipart ? parseMultipart(bytes, boundary?.[1] || boundary?.[2]) : [{ ...contentRange(response.headers.get("Content-Range")), data: bytes }];
        const etag = response.headers.get("ETag");
        const currentValidator = etag && !etag.startsWith("W/") ? etag : response.headers.get("Last-Modified");
        if (validator && currentValidator && validator !== currentValidator) throw new Error("Media resource changed during download");
        if (!validator) validator = currentValidator;
        parts.sort((a, b) => a.start - b.start);
        for (const [index, part] of parts.entries()) {
          if (total !== null && part.total !== total) throw new Error("Media size changed during download");
          total = part.total;
          if (part.data.length !== part.end - part.start) throw new Error("Truncated range payload");
          if (index && part.start < parts[index - 1].end) throw new Error("Overlapping range response");
          if (!requested.some(r => part.start < r.end && part.end > r.start)) throw new Error("Unrequested range response");
        }
        for (const range of requested) {
          let next = range.start;
          for (const part of parts) if (part.start <= next && part.end > next) next = part.end;
          if (next < Math.min(range.end, total)) throw new Error("Incomplete range response");
        }
        return parts;
      } finally {
        if (streamReader) {
          await streamReader.cancel().catch(() => {});
          streamReader.releaseLock();
        } else await response.body?.cancel().catch(() => {});
      }
    },
  };
}

export function planAudioRanges(samples, total, { maxGap = 0, maxSpan = 4 * MIB } = {}) {
  if (!Number.isSafeInteger(maxGap) || maxGap < 0 || maxGap > 65536) throw new Error("Invalid range gap");
  const sorted = samples.map(sample => {
    const start = sample.offset;
    const end = start + sample.size;
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || sample.size <= 0 || end > total) throw new Error("Invalid audio sample offsets");
    return { start, end };
  }).sort((a, b) => a.start - b.start);
  const ranges = [];
  for (const range of sorted) {
    const previous = ranges.at(-1);
    if (previous && range.start - previous.end <= maxGap && range.end - previous.start <= maxSpan) previous.end = Math.max(previous.end, range.end);
    else ranges.push({ ...range });
  }
  return ranges;
}

function remainingRanges(ranges, cached) {
  const sorted = [...cached].sort((a, b) => a.start - b.start);
  return ranges.flatMap(range => {
    const result = [];
    let start = range.start;
    for (const block of sorted) {
      if (block.start >= range.end) break;
      if (block.end <= start) continue;
      if (block.start > start) result.push({ start, end: block.start });
      start = Math.max(start, block.end);
    }
    if (start < range.end) result.push({ start, end: range.end });
    return result;
  });
}

function append(file, part) {
  const buffer = part.data.buffer.slice(part.data.byteOffset, part.data.byteOffset + part.data.byteLength);
  buffer.fileStart = part.start;
  return file.appendBuffer(buffer);
}

export async function inspectAudio(url, options = {}) {
  const reader = createRangeReader(url, options);
  const file = createFile(true);
  const cached = [];
  const chunkSize = options.chunkSize || 64 * 1024;
  let info;
  let error;
  let offset = 0;
  file.onReady = value => { info = value; };
  file.onError = message => { error = new Error(`MP4 parsing failed: ${message}`); };
  try {
    for (let step = 0; !info; step++) {
      if (step >= 512 || reader.stats.transferred_bytes >= 16 * MIB) throw new Error("MP4 index exceeds probe budget");
      const parts = await reader.read([{ start: offset, end: offset + chunkSize }]);
      cached.push(...parts.map(({ start, end }) => ({ start, end })));
      for (const part of parts) offset = append(file, part);
      if (error) throw error;
      if (!info && (!Number.isSafeInteger(offset) || offset >= reader.total || offset <= parts[0].start)) throw new Error("Cannot locate complete MP4 index");
    }
    if (info.isFragmented || file.moov?.mvex) throw new Error("Fragmented MP4 is outside this experiment");
    const track = info.audioTracks.find(item => item.codec.startsWith("mp4a.40."));
    if (!track || !track.nb_samples) throw new Error("No supported AAC track");
    const samples = file.getTrackSamplesInfo(track.id);
    const plans = [0, 1024, 4096, 65536].map(maxGap => {
      const ranges = remainingRanges(planAudioRanges(samples, reader.total, { maxGap }), cached);
      return { max_gap: maxGap, ranges: ranges.length, requests_128: Math.ceil(ranges.length / 128), payload_bytes: ranges.reduce((sum, r) => sum + r.end - r.start, 0) };
    });
    return { file, reader, track, samples, cached, summary: {
      media_bytes: reader.total, metadata_bytes: reader.stats.transferred_bytes, metadata_requests: reader.stats.requests,
      audio_payload_bytes: samples.reduce((sum, sample) => sum + sample.size, 0),
      duration_seconds: track.duration / track.timescale, codec: track.codec, plans,
    } };
  } catch (error) {
    file.stop();
    throw error;
  }
}

export async function downloadIndexedAudio(index, { batchSize = 128, concurrency = 4, maxGap = 0, signal } = {}) {
  if (!Number.isInteger(batchSize) || batchSize < 1 || batchSize > 128 || !Number.isInteger(concurrency) || concurrency < 1 || concurrency > 8) throw new Error("Invalid range batching options");
  const { file, reader, track, samples, cached } = index;
  const ranges = remainingRanges(planAudioRanges(samples, reader.total, { maxGap }), cached);
  const batches = [];
  for (let i = 0; i < ranges.length; i += batchSize) batches.push(ranges.slice(i, i + batchSize));
  const chunks = [];
  let extracted = 0;
  let failure;
  file.onError = message => { failure = new Error(`MP4 parsing failed: ${message}`); };
  file.onSegment = (id, user, buffer, count) => {
    chunks.push(buffer);
    extracted = count;
    file.releaseUsedSamples(id, count);
  };
  try {
    signal?.throwIfAborted();
    file.ftyp.major_brand = "M4A ";
    file.ftyp.compatible_brands = ["M4A ", "isom", "iso6"];
    file.setSegmentOptions(track.id, null, { nbSamples: 1000, rapAlignement: false });
    chunks.push(file.initializeSegmentation().buffer);
    file.start();
    for (let i = 0; i < batches.length; i += concurrency) {
      signal?.throwIfAborted();
      const results = await Promise.allSettled(batches.slice(i, i + concurrency).map(batch => reader.read(batch)));
      for (const result of results) {
        if (result.status === "rejected") throw result.reason;
        for (const part of result.value) append(file, part);
        if (failure) throw failure;
      }
    }
    file.flush();
    if (failure) throw failure;
    if (extracted !== track.nb_samples) throw new Error(`Incomplete audio track (${extracted}/${track.nb_samples})`);
    const blob = new Blob(chunks, { type: "audio/x-m4a" });
    return { blob, summary: { ...index.summary, ...reader.stats, audio_file_bytes: blob.size,
      saved_percent: 100 * (1 - reader.stats.transferred_bytes / reader.total), audio_requests: batches.length,
      batch_size: batchSize, concurrency, max_gap: maxGap } };
  } finally {
    file.stop();
    chunks.length = 0;
  }
}

async function main() {
  const args = process.argv.slice(2);
  if (!args.length || args.includes("--help")) {
    console.log("Usage: node scripts/audio-range.mjs --manifest PATH --sub-id ID [--output FILE.m4a] [--gap-bytes 0] [--batch-size 128] [--concurrency 4]\nWithout --output, only fetch and inspect the MP4 index. No full-download fallback.");
    return;
  }
  const options = {};
  for (let i = 0; i < args.length; i += 2) {
    if (!["--manifest", "--sub-id", "--output", "--gap-bytes", "--batch-size", "--concurrency"].includes(args[i]) || !args[i + 1] || options[args[i]]) throw new Error("Invalid arguments; use --help");
    options[args[i]] = args[i + 1];
  }
  if (!options["--manifest"] || !/^\d+$/.test(options["--sub-id"] || "")) throw new Error("--manifest and numeric --sub-id are required");
  const maxGap = Number(options["--gap-bytes"] || 0);
  const batchSize = Number(options["--batch-size"] || 128);
  const concurrency = Number(options["--concurrency"] || 4);
  if (!Number.isInteger(maxGap) || maxGap < 0 || maxGap > 65536 || !Number.isInteger(batchSize) || batchSize < 1 || batchSize > 128 || !Number.isInteger(concurrency) || concurrency < 1 || concurrency > 8) throw new Error("Invalid range options");
  if (options["--output"] && await access(options["--output"]).then(() => true, error => {
    if (error.code === "ENOENT") return false;
    throw error;
  })) throw new Error("Output already exists; choose another file");
  const manifest = JSON.parse(await readFile(options["--manifest"], "utf8"));
  const entry = manifest.items?.find(item => String(item.sub_id) === options["--sub-id"]);
  if (!entry?.media_url) throw new Error("Selected lesson has no media_url in the manifest");
  const controller = new AbortController();
  process.once("SIGINT", () => controller.abort(new Error("Cancelled")));
  const started = performance.now();
  const index = await inspectAudio(entry.media_url, { signal: controller.signal });
  let summary = index.summary;
  if (options["--output"]) {
    const output = resolve(options["--output"]);
    const result = await downloadIndexedAudio(index, { signal: controller.signal, maxGap, batchSize, concurrency });
    await mkdir(dirname(output), { recursive: true });
    await writeFile(output, Buffer.from(await result.blob.arrayBuffer()), { flag: "wx" });
    summary = result.summary;
  } else index.file.stop();
  console.log(JSON.stringify({ ...summary, elapsed_seconds: (performance.now() - started) / 1000 }, null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => {
    // Network errors may include the input URL: only print the error class here.
    console.error(error.name === "TypeError" ? "Network request failed" : error.message);
    process.exitCode = 1;
  });
}
