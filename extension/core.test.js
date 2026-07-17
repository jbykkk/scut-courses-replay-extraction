const test = require("node:test");
const assert = require("node:assert/strict");

const {
  parseCourseUrl,
  createApi,
  extractSubtitleItems,
  toSrt,
  toTxt,
} = require("./core.js");

const INTERNAL = "https://video.jw.scut.edu.cn";
const EXTERNAL = "https://video-jw-443.webvpn.scut.edu.cn";

test("parses internal and external course URLs", () => {
  assert.equal(parseCourseUrl(`${INTERNAL}/livingroom?course_id=1&sub_id=2`).siteKey, "internal");
  const external = parseCourseUrl(`${EXTERNAL}/livingroom/?sub_id=2&tenant_code=99&course_id=1`);
  assert.equal(external.siteKey, "external");
  assert.equal(external.origin, EXTERNAL);
  assert.equal(external.tenantCode, "99");
});

test("rejects unsafe and malformed URLs", () => {
  assert.equal(parseCourseUrl("https://example.com/livingroom?sub_id=2"), null);
  assert.equal(parseCourseUrl("http://video.jw.scut.edu.cn/livingroom?sub_id=2"), null);
  assert.equal(parseCourseUrl(`${INTERNAL}/livingroom?course_id=1`), null);
  assert.equal(parseCourseUrl(`${INTERNAL}/other?sub_id=2`), null);
});

test("builds API URLs on the selected origin with encoded parameters", () => {
  const api = createApi(EXTERNAL);
  assert.equal(new URL(api.subtitle("a b")).origin, EXTERNAL);
  assert.equal(new URL(api.subtitle("a b")).searchParams.get("sub_id"), "a b");
  assert.equal(new URL(api.catalogue("1")).pathname, "/courseapi/v2/course/catalogue");
});

test("extracts nested subtitles and rejects unrelated arrays", () => {
  const items = [{ BeginSec: 1, Text: "你好" }];
  assert.deepEqual(extractSubtitleItems({ list: [{ all_content: items }] }), items);
  assert.deepEqual(extractSubtitleItems({ data: [{ name: "other" }] }), []);
});

test("formats valid subtitles consistently", () => {
  const items = [
    { BeginSec: 1, EndSec: 2, Text: "第一条" },
    { BeginSec: 2, EndSec: 3, Text: "" },
    { BeginSec: "bad", Text: "错误" },
    { BeginSec: 5.25, Text: "第二条" },
  ];
  const srt = toSrt(items);
  assert.match(srt, /1\n00:00:01,000 --> 00:00:02,000\n第一条/);
  assert.match(srt, /2\n00:00:05,250 --> 00:00:10,250\n第二条/);
  assert.equal(toTxt(items), "第一条\n第二条");
});
