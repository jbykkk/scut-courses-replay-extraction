const assert = require("node:assert/strict");
const {test} = require("node:test");
const {safeReplay} = require("./study-specialized.js");
const {bookmark, seek} = require("./replay-seek.js");
const url = "https://video.jw.scut.edu.cn/livingroom?course_id=10&sub_id=2&note_play=300";
test("replay URL is scoped to a real school route and numeric seek", () => {
  assert.equal(safeReplay(url), url); assert.equal(bookmark(url).seconds, 300);
  for (const bad of ["javascript:alert(1)", url.replace("video.jw.scut.edu.cn", "example.test"), url.replace("300", "NaN"), url.replace("300", "-1"), url.replace("https:", "http:"), url.replace("livingroom", "login")]) {
    assert.equal(safeReplay(bad), ""); assert.equal(bookmark(bad), null);
  }
  assert.notEqual(bookmark(url).lesson, bookmark(url.replace("sub_id=2", "sub_id=3")).lesson);
});
test("seek waits for video metadata and never starts playback or seeks live streams", () => {
  let played = false;
  const video = {readyState:0,duration:900,currentTime:0,play(){played=true;}};
  assert.equal(seek(video,300),false); assert.equal(video.currentTime,0);
  video.readyState=1; assert.equal(seek(video,300),true); assert.equal(video.currentTime,300);
  assert.equal(played,false); assert.equal(seek(video,901),false);
  video.duration=Infinity; assert.equal(seek(video,300),false);
});
