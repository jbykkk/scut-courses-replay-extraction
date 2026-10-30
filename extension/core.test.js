const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  parseCourseUrl,
  createApi,
  extractSubtitleItems,
  toSrt,
  toTxt,
  sanitizeFilename,
  sanitizeIdForFilename,
} = require("./core.js");

const INTERNAL = "https://video.jw.scut.edu.cn";
const EXTERNAL = "https://video-jw-443.webvpn.scut.edu.cn";
const fixtures = path.join(__dirname, "..", "tests", "fixtures");
const urlCases = JSON.parse(
  fs.readFileSync(path.join(fixtures, "url_cases.json"), "utf8")
);
const subtitleCases = JSON.parse(
  fs.readFileSync(path.join(fixtures, "subtitle_cases.json"), "utf8")
);

test("Python and JavaScript share strict course URL cases", () => {
  for (const item of urlCases) {
    const parsed = parseCourseUrl(item.url);
    if (!item.valid) {
      assert.equal(parsed, null, item.name);
      continue;
    }
    assert.equal(parsed.siteKey, item.site_key, item.name);
    assert.equal(parsed.courseId, item.course_id, item.name);
    assert.equal(parsed.subId, item.sub_id, item.name);
    assert.equal(parsed.tenantCode, item.tenant_code, item.name);
  }
});

test("builds API URLs on the selected origin with encoded parameters", () => {
  const api = createApi(EXTERNAL);
  assert.equal(new URL(api.subtitle("123")).origin, EXTERNAL);
  assert.equal(new URL(api.subtitle("123")).searchParams.get("sub_id"), "123");
  assert.equal(new URL(api.catalogue("1")).pathname, "/courseapi/v2/course/catalogue");
  assert.throws(() => api.subtitle("../../escaped"), /sub_id/);
  assert.throws(() => api.subInfo("../1", "2"), /course_id/);
  assert.throws(() => createApi("https://example.com"), /origin/);
});

test("extracts nested subtitles and rejects unrelated arrays", () => {
  const items = [{ BeginSec: 1, Text: "你好" }];
  assert.deepEqual(extractSubtitleItems({ list: [{ all_content: items }] }), items);
  assert.deepEqual(extractSubtitleItems({ data: [{ name: "other" }] }), []);
});

test("Python and JavaScript share subtitle conversion vectors", () => {
  assert.equal(toSrt(subtitleCases.items), subtitleCases.srt);
  assert.equal(toTxt(subtitleCases.items), subtitleCases.txt);
});

test("filename and ZIP entry components are sanitized again", () => {
  assert.equal(sanitizeIdForFilename("550530", "sub_id"), "550530");
  assert.throws(() => sanitizeIdForFilename("../../escaped", "sub_id"), /sub_id/);
  const cleaned = sanitizeFilename("../../evil:\\name\u0000");
  assert.equal(cleaned.includes("/"), false);
  assert.equal(cleaned.includes("\\"), false);
  assert.equal(cleaned.includes(":"), false);
  assert.equal(cleaned.includes("\u0000"), false);
});

const { parseTask, mediaFromInfo, audioFilename } = require("./audio-core.js");

test("audio tasks validate origin, IDs and duplicate parameters", () => {
  const query = `?origin=${encodeURIComponent(EXTERNAL)}&course_id=42&sub_ids=1,1,2&tab_id=7`;
  assert.deepEqual(parseTask(query).subIds, ["1", "2"]);
  assert.throws(() => parseTask(query + "&course_id=43"));
  assert.throws(() => parseTask(query.replace("sub_ids=1,1,2", "sub_ids=../1")));
  assert.throws(() => parseTask(query.replace(encodeURIComponent(EXTERNAL), "https://example.com")));
  assert.equal(audioFilename("42", "1", "2026-10-16第3-4节"), "audio_42/2026-10-16_1.m4a");
  assert.equal(audioFilename("42", "1", "../../evil").split("/").length, 2);
});

test("audio media preference matches CLI and WebVPN rewrites internal URLs", () => {
  const media = `${INTERNAL}/recording.mp4`;
  const data = { content: { save_playback: { contents: media } }, video_list: [
    { type: 4, preview_url: `${media}?student` }, { type: 3, preview_url: `${media}?teacher` }
  ] };
  assert.equal(mediaFromInfo(data, INTERNAL).field, "content.save_playback.contents");
  assert.equal(new URL(mediaFromInfo(data, EXTERNAL).url).origin, EXTERNAL);
  delete data.content;
  assert.equal(mediaFromInfo(data, INTERNAL).url, media + "?teacher");
  assert.throws(() => mediaFromInfo({ playurl: { hd: "file:///tmp/a.mp4" } }, INTERNAL));
  assert.throws(() => mediaFromInfo({ is_m3u8: "yes", playurl: { hd: media } }, INTERNAL), /HLS/);
});

test("browser MP4 extraction keeps only AAC and rejects truncated input", async () => {
  const { createAudioExtractor } = await import("./mp4-audio.mjs");
  const { createFile } = await import("./vendor/mp4box.all.mjs");
  const bytes = fs.readFileSync(path.join(fixtures, "replay-aac.mp4"));
  const extractor = createAudioExtractor();
  for (let offset = 0; offset < bytes.length; offset += 129) {
    extractor.append(bytes.subarray(offset, offset + 129), offset);
  }
  const { blob, duration } = extractor.finish();
  assert.ok(duration > 2 && duration < 2.1);
  const output = createFile(true);
  let tracks;
  output.onReady = (info) => { tracks = info.tracks; };
  const buffer = await blob.arrayBuffer();
  assert.equal(new TextDecoder().decode(buffer.slice(8, 12)), "M4A ");
  buffer.fileStart = 0;
  output.appendBuffer(buffer);
  assert.equal(tracks.length, 1);
  assert.ok(tracks[0].audio);
  assert.equal(tracks[0].video, undefined);
  extractor.dispose();
  const truncated = createAudioExtractor();
  truncated.append(bytes.subarray(0, 100), 0);
  assert.throws(() => truncated.finish(), /完整 MP4/);
  truncated.dispose();
});
