import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { createFile } from "../extension/vendor/mp4box.all.mjs";
import { createRangeReader, parseMultipart, planAudioRanges, inspectAudio, downloadIndexedAudio } from "../scripts/audio-range.mjs";
import { extractAudio } from "../extension/mp4-audio.mjs";

const fixture = await readFile(new URL("./fixtures/replay-aac.mp4", import.meta.url));
const interleavedFixture = await readFile(new URL("./fixtures/replay-interleaved-aac.mp4", import.meta.url));

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
  const pendingResponses = new Set();
  const pendingStarted = Promise.withResolvers();
  const pendingStopped = Promise.withResolvers();
  let wireBytes = 0;
  const server = createServer((req, res) => {
    if (!req.headers.range) {
      requests.push([]);
      wireBytes += data.length;
      res.writeHead(200, { "Content-Length": data.length });
      res.end(data);
      return;
    }
    const ranges = req.headers.range.replace("bytes=", "").split(",").map(value => {
      const [start, last] = value.split("-").map(Number);
      return { start, end: Math.min(last + 1, data.length) };
    });
    requests.push(ranges);
    if (mode === "denied") { res.writeHead(403); res.end(); return; }
    if (mode === "oversized") {
      res.writeHead(206, { "Content-Range": "bytes 0-31/1073741825" });
      res.end(data.subarray(0, 32));
      return;
    }
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
    if (mode === "paused-multi" && ranges.length > 1) {
      pendingResponses.add(res);
      res.on("close", () => {
        pendingResponses.delete(res);
        if (!pendingResponses.size) pendingStopped.resolve();
      });
      res.writeHead(206, { "Content-Type": "multipart/byteranges; boundary=pending" });
      res.flushHeaders();
      if (pendingResponses.size === 4) pendingStarted.resolve();
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
  return {
    url: `http://127.0.0.1:${server.address().port}/recording.mp4`, requests,
    pendingStarted: pendingStarted.promise, pendingStopped: pendingStopped.promise,
    get pendingCount() { return pendingResponses.size; },
    get wireBytes() { return wireBytes; },
  };
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
  assert.deepEqual(Buffer.from(parseMultipart(body, "range-boundary")[0].data), data);
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

function padMediaData(data, padding = 80 * 1024) {
  const file = createFile(true);
  const buffer = Uint8Array.from(data).buffer;
  buffer.fileStart = 0;
  file.appendBuffer(buffer);
  const changed = Buffer.from(data);
  const mdat = file.mdats[0];
  changed.writeUInt32BE(mdat.size + padding, mdat.start);
  for (const track of file.moov.traks) {
    const table = track.mdia.minf.stbl.stco;
    table.chunk_offsets.forEach((offset, index) => changed.writeUInt32BE(offset + padding, table.start + 16 + 4 * index));
  }
  const insert = mdat.start + mdat.hdr_size;
  return Buffer.concat([changed.subarray(0, insert), Buffer.alloc(padding), changed.subarray(insert)]);
}

function moveMoovToFront(data) {
  const file = createFile(true);
  const buffer = Uint8Array.from(data).buffer;
  buffer.fileStart = 0;
  file.appendBuffer(buffer);
  const moov = file.moov;
  const insert = file.ftyp.start + file.ftyp.size;
  assert.ok(moov.start > file.mdats[0].start, "source fixture must have trailing metadata");
  const metadata = Buffer.from(data.subarray(moov.start, moov.start + moov.size));
  for (const track of moov.traks) {
    const table = track.mdia.minf.stbl.stco;
    for (const [index, offset] of table.chunk_offsets.entries()) {
      assert.ok(offset >= insert && offset < moov.start);
      metadata.writeUInt32BE(offset + moov.size, table.start - moov.start + 16 + 4 * index);
    }
  }
  return Buffer.concat([
    data.subarray(0, insert), metadata, data.subarray(insert, moov.start), data.subarray(moov.start + moov.size),
  ]);
}

for (const layout of ["trailing", "leading"]) {
  test(`selective downloads skip bulk media with ${layout} metadata`, async t => {
    const padded = padMediaData(fixture, 2 * 1024 * 1024);
    const data = layout === "leading" ? moveMoovToFront(padded) : padded;
    const media = await server(t, "normal", data);
    const result = await extractAudio(media.url);
    assert.equal(result.method, "ranges");
    assert.ok(media.requests.some(ranges => ranges.length > 1));
    assert.ok(media.requests.every(ranges => ranges.length), "must not use full-download fallback");
    assert.equal(result.transferredBytes, media.wireBytes);
    assert.equal(result.mediaBytes, data.length);
    assert.ok(media.wireBytes < data.length / 4, "response bodies must stay below 25% of this padded fixture");
    const actual = audioPackets(new Uint8Array(await result.blob.arrayBuffer()));
    assert.equal(actual.info.videoTracks.length, 0);
    assert.deepEqual(actual.bytes, audioPackets(fixture).bytes);
  });
}

test("plugin prefers selective extraction and reports actual transfer statistics", async t => {
  const media = await server(t);
  const result = await extractAudio(media.url);
  assert.equal(result.method, "ranges");
  assert.equal(result.transferredBytes, media.wireBytes);
  assert.deepEqual(audioPackets(new Uint8Array(await result.blob.arrayBuffer())).bytes, audioPackets(fixture).bytes);
});

test("plugin falls back once when multipart is unsupported after a successful index", async t => {
  const data = padMediaData(fixture);
  const media = await server(t, "ignored-multi", data);
  const result = await extractAudio(media.url);
  assert.equal(result.method, "full");
  assert.match(result.fallbackReason, /HTTP 200/);
  assert.ok(media.requests.some(ranges => ranges.length > 1));
  assert.equal(media.requests.filter(ranges => !ranges.length).length, 1);
  assert.ok(result.transferredBytes > data.length);
  assert.deepEqual(audioPackets(new Uint8Array(await result.blob.arrayBuffer())).bytes, audioPackets(fixture).bytes);
});

test("plugin preserves the original download when even the first Range is ignored", async t => {
  const media = await server(t, "ignored");
  const result = await extractAudio(media.url);
  assert.equal(result.method, "full");
  assert.equal(result.transferredBytes, fixture.length);
  assert.equal(media.requests.filter(ranges => !ranges.length).length, 1);
});

test("plugin does not retry authentication failures or bypass the media limit", async t => {
  const denied = await server(t, "denied");
  await assert.rejects(extractAudio(denied.url), /HTTP 403/);
  assert.equal(denied.requests.length, 1);
  const oversized = await server(t, "oversized");
  await assert.rejects(extractAudio(oversized.url), /超过 1 GB/);
  assert.equal(oversized.requests.length, 1);
});

test("cancelling selective plugin reads never starts the full-download fallback", async t => {
  const media = await server(t, "paused");
  const controller = new AbortController();
  const result = extractAudio(media.url, { signal: controller.signal });
  setTimeout(() => controller.abort(), 30);
  await assert.rejects(result, /abort/i);
  assert.ok(media.requests.every(ranges => ranges.length));
});

test("cancelling an active concurrent batch closes all requests without full fallback", { timeout: 5000 }, async t => {
  const data = padMediaData(interleavedFixture);
  const media = await server(t, "paused-multi", data);
  const controller = new AbortController();
  const result = extractAudio(media.url, { signal: controller.signal });
  const rejected = assert.rejects(result, /abort/i);
  await media.pendingStarted;
  assert.equal(media.pendingCount, 4);
  controller.abort();
  await rejected;
  await media.pendingStopped;
  assert.equal(media.pendingCount, 0);
  assert.equal(media.requests.filter(ranges => ranges.length > 1).length, 4);
  assert.ok(media.requests.every(ranges => ranges.length), "cancellation must not start a full download");
});
