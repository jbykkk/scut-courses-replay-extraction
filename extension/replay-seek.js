/* The note_play bookmark is scoped to the exact lesson. Never starts playback. */
(() => {
  function bookmark(url) {
    try { const u = new URL(url), p = u.searchParams;
      if (u.protocol !== "https:" || !["video.jw.scut.edu.cn", "video-jw-443.webvpn.scut.edu.cn"].includes(u.host) || !/^\/livingroom\/?$/.test(u.pathname) || u.username || u.password) return null;
      if (!["course_id", "sub_id", "note_play"].every(k => /^\d+$/.test(p.get(k) || ""))) return null;
      const seconds = Number(p.get("note_play")); if (!Number.isSafeInteger(seconds) || seconds > 86400) return null;
      return {lesson: u.origin + "/" + p.get("course_id") + "/" + p.get("sub_id"), seconds};
    } catch { return null; }
  }
  function seek(video, seconds) {
    if (video.readyState < 1 || !Number.isFinite(video.duration) || seconds >= video.duration || video.duration < 5) return false;
    try { video.currentTime = seconds; return Math.abs(video.currentTime - seconds) < 1; } catch { return false; }
  }
  if (typeof module !== "undefined") module.exports = {bookmark, seek};
  if (typeof document === "undefined") return;
  const target = bookmark(location.href); if (!target) return;
  let polls = 0, stable = 0;
  const timer = setInterval(() => {
    if (bookmark(location.href)?.lesson !== target.lesson || ++polls > 90) { clearInterval(timer); return; }
    const videos = [...document.querySelectorAll("video")].filter(v => v.readyState >= 1 && Number.isFinite(v.duration) && v.duration > target.seconds);
    if (!videos.length) return;
    if (videos.every(v => Math.abs(v.currentTime - target.seconds) < 2)) { if (++stable >= 3) clearInterval(timer); return; }
    stable = 0;
    for (const video of videos) seek(video, target.seconds);
  }, 500);
  addEventListener("pagehide", () => clearInterval(timer), {once:true});
})();
