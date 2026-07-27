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
