import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { createFile } from "../extension/vendor/mp4box.all.mjs";
import { createRangeReader, parseMultipart, planAudioRanges, inspectAudio, downloadIndexedAudio } from "../scripts/audio-range.mjs";

const fixture = await readFile(new URL("./fixtures/replay-aac.mp4", import.meta.url));

function multipart(ranges, data, boundary = "range-boundary") {
  return Buffer.concat([
    ...ranges.flatMap(({ start, end }) => [
      Buffer.from(`\r\n--${boundary}\r\nContent-Type: video/mp4\r\nContent-Range: bytes ${start}-${end - 1}/${data.length}\r\n\r\n`),
      data.subarray(start, end),
    ]), Buffer.from(`\r\n--${boundary}--\r\n`),
  ]);
}

async function server(t, mode = "normal", data = fixture) {
  const requests = [];
  let wireBytes = 0;
  const server = createServer((req, res) => {
    const ranges = req.headers.range.replace("bytes=", "").split(",").map(value => {
      const [start, last] = value.split("-").map(Number);
      return { start, end: Math.min(last + 1, data.length) };
    });
    requests.push(ranges);
    res.setHeader("ETag", mode === "changed" && requests.length > 1 ? '"new"' : '"stable"');
    if (mode === "ignored" || (mode === "ignored-multi" && ranges.length > 1)) {
      res.writeHead(200, { "Content-Length": data.length });
      res.end(data);
      return;
    }
    if (mode === "budget") {
      res.writeHead(206, { "Content-Length": 100000000 });
      res.end();
      return;
    }
    if (mode === "paused") {
      res.writeHead(206, { "Content-Range": `bytes 0-255/${data.length}`, "Content-Length": 256 });
      res.flushHeaders();
      return;
    }
    let body;
    if (ranges.length > 1) {
      body = multipart(mode === "missing" ? ranges.slice(1) : ranges, data);
      res.setHeader("Content-Type", 'multipart/byteranges; boundary="range-boundary"');
    } else {
      const { start, end } = ranges[0];
      res.setHeader("Content-Range", `bytes ${start}-${end - 1}/${data.length}`);
      body = data.subarray(start, end);
    }
    res.writeHead(206, { "Content-Length": body.length });
    wireBytes += body.length;
    res.end(body);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => {
    server.closeAllConnections();
    return new Promise(resolve => server.close(resolve));
  });
  return { url: `http://127.0.0.1:${server.address().port}/recording.mp4`, requests, get wireBytes() { return wireBytes; } };
}

function audioPackets(bytes) {
  const file = createFile(true);
  const buffer = Uint8Array.from(bytes).buffer;
  buffer.fileStart = 0;
  file.appendBuffer(buffer);
  const info = file.getInfo();
  const track = info.audioTracks[0];
  return { info, bytes: Buffer.concat(file.getTrackSamplesInfo(track.id).map((_, i) => Buffer.from(file.getTrackSample(track.id, i).data))) };
}

test("multipart parser preserves binary data and validates declared lengths", () => {
  const data = Buffer.from("\0\r\n--range-boundary\r\n\xffdata", "latin1");
  const body = multipart([{ start: 0, end: data.length }], data);
  assert.deepEqual(parseMultipart(body, "range-boundary")[0].data, data);
  assert.throws(() => parseMultipart(body.subarray(0, body.length - 10), "range-boundary"), /multipart/);
  assert.throws(() => parseMultipart(body, "missing"), /boundary/);
});

test("audio range planning merges nearby samples and rejects invalid offsets", () => {
  const samples = [{ offset: 30, size: 5 }, { offset: 10, size: 5 }, { offset: 15, size: 5 }];
  assert.deepEqual(planAudioRanges(samples, 100), [{ start: 10, end: 20 }, { start: 30, end: 35 }]);
  assert.deepEqual(planAudioRanges(samples, 100, { maxGap: 10 }), [{ start: 10, end: 35 }]);
  assert.throws(() => planAudioRanges([{ offset: 95, size: 10 }], 100), /offsets/);
});

test("sparse index and multipart audio extraction preserve every AAC packet", async t => {
  const media = await server(t);
  const index = await inspectAudio(media.url, { chunkSize: 256 });
  assert.ok(index.summary.metadata_bytes < fixture.length);
  assert.ok(index.file.moov.start > fixture.length / 2, "fixture metadata must be at the end");
  const { blob, summary } = await downloadIndexedAudio(index, { batchSize: 8, concurrency: 3 });
  assert.ok(media.requests.some(ranges => ranges.length > 1));
  assert.equal(summary.transferred_bytes, media.wireBytes);
  const actual = audioPackets(new Uint8Array(await blob.arrayBuffer()));
  assert.equal(actual.info.videoTracks.length, 0);
  assert.equal(actual.info.audioTracks.length, 1);
  assert.deepEqual(actual.bytes, audioPackets(fixture).bytes);
});

test("single-range mode also extracts the complete audio track", async t => {
  const media = await server(t);
  const index = await inspectAudio(media.url, { chunkSize: 512 });
  const { blob } = await downloadIndexedAudio(index, { batchSize: 1 });
  assert.deepEqual(audioPackets(new Uint8Array(await blob.arrayBuffer())).bytes, audioPackets(fixture).bytes);
});

test("coalescing audio ranges preserves AAC and fragmented input is rejected", async t => {
  const media = await server(t);
  const index = await inspectAudio(media.url, { chunkSize: 256 });
  const { blob } = await downloadIndexedAudio(index, { maxGap: 1024, batchSize: 8 });
  const data = Buffer.from(await blob.arrayBuffer());
  assert.deepEqual(audioPackets(data).bytes, audioPackets(fixture).bytes);
  const fragmented = await server(t, "normal", data);
  await assert.rejects(inspectAudio(fragmented.url, { chunkSize: 256 }), /Fragmented/);
});

test("ignoring Range fails before consuming an entire media response", async t => {
  const media = await server(t, "ignored");
  const reader = createRangeReader(media.url);
  await assert.rejects(reader.read([{ start: 0, end: 32 }]), /HTTP 200/);
  assert.equal(reader.stats.transferred_bytes, 0);
});

test("ignoring multipart requests does not trigger full-video fallback", async t => {
  const media = await server(t, "ignored-multi");
  const index = await inspectAudio(media.url, { chunkSize: 256 });
  await assert.rejects(downloadIndexedAudio(index, { batchSize: 8 }), /HTTP 200/);
});

test("missing requested parts and changed validators are rejected", async t => {
  const missing = await server(t, "missing");
  await assert.rejects(createRangeReader(missing.url).read([{ start: 0, end: 10 }, { start: 20, end: 30 }]), /Incomplete/);
  const changed = await server(t, "changed");
  const reader = createRangeReader(changed.url);
  await reader.read([{ start: 0, end: 10 }]);
  await assert.rejects(reader.read([{ start: 20, end: 30 }]), /changed/);
});

test("oversized responses and cancellation stop network reads", async t => {
  const oversized = await server(t, "budget");
  await assert.rejects(createRangeReader(oversized.url).read([{ start: 0, end: 32 }]), /budget/);
  const paused = await server(t, "paused");
  const controller = new AbortController();
  const result = createRangeReader(paused.url, { signal: controller.signal }).read([{ start: 0, end: 256 }]);
  setTimeout(() => controller.abort(), 30);
  await assert.rejects(result, /abort/i);
});
