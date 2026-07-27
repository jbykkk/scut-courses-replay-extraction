"""Run an authenticated, unpacked-extension smoke test and save evidence.

This script intentionally aborts the popup's first subtitle request. A successful
download therefore proves that popup.js retried once inside the course page.
It never prints or copies cookies into the evidence directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from pathlib import PurePosixPath

from playwright.async_api import BrowserContext, Page, Route, async_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auth import get_auth_path
from config import SUBTITLE_API_PATH, get_site, parse_course_url


EXTENSION_DIR = ROOT / "extension"
DEFAULT_EVIDENCE_DIR = ROOT / "evidence"


async def find_extension_id(profile_dir: Path) -> str:
    """Find the unpacked extension ID without exposing profile contents."""
    extension_dir = EXTENSION_DIR.resolve()
    for _ in range(50):
        for preferences_name in ("Preferences", "Secure Preferences"):
            preferences_path = profile_dir / "Default" / preferences_name
            if not preferences_path.is_file():
                continue
            try:
                preferences = json.loads(
                    preferences_path.read_text(encoding="utf-8")
                )
            except (json.JSONDecodeError, OSError):
                continue
            settings = preferences.get("extensions", {}).get("settings", {})
            for extension_id, details in settings.items():
                path = details.get("path")
                if not path:
                    continue
                try:
                    if Path(path).resolve() == extension_dir:
                        return extension_id
                except OSError:
                    continue
        await asyncio.sleep(0.1)
    raise RuntimeError("无法从临时 Chrome 配置中识别已加载扩展的 ID。")


async def restore_storage(context: BrowserContext, state_path: Path) -> None:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    cookies = state.get("cookies", [])
    if cookies:
        await context.add_cookies(cookies)

    for origin_state in state.get("origins", []):
        origin = origin_state.get("origin")
        entries = origin_state.get("localStorage", [])
        if not origin or not entries:
            continue
        page = await context.new_page()
        try:
            await page.goto(origin, wait_until="domcontentloaded")
            await page.evaluate(
                """entries => {
                    for (const {name, value} of entries) {
                        localStorage.setItem(name, value);
                    }
                }""",
                entries,
            )
        finally:
            await page.close()


async def open_popup_for_course(
    context: BrowserContext,
    course_page: Page,
    popup_url: str,
) -> Page:
    """Load popup.html in a background tab while the course tab stays active."""
    popup_page = await context.new_page()
    popup_script_requested = asyncio.Event()
    release_popup_script = asyncio.Event()

    async def delay_popup_script(route: Route) -> None:
        popup_script_requested.set()
        await release_popup_script.wait()
        await route.continue_()

    await popup_page.route("**/popup.js", delay_popup_script)
    navigation = asyncio.create_task(
        popup_page.goto(popup_url, wait_until="load", timeout=30_000)
    )
    await asyncio.wait_for(popup_script_requested.wait(), timeout=10)
    await course_page.bring_to_front()
    release_popup_script.set()
    await navigation
    return popup_page


async def get_download_items(popup_page: Page) -> list[dict]:
    return await popup_page.evaluate(
        """() => new Promise((resolve, reject) => {
            chrome.downloads.search(
                {},
                items => {
                    const error = chrome.runtime.lastError;
                    if (error) reject(new Error(error.message));
                    else resolve(items.map(item => ({
                        id: item.id,
                        filename: item.filename,
                        state: item.state,
                    })));
                }
            );
        })"""
    )


async def run_check(
    course_url: str,
    evidence_dir: Path,
    *,
    check_batch: bool = False,
) -> Path:
    course = parse_course_url(course_url)
    site = get_site(course.site_key)
    auth_path = get_auth_path(course.site_key)
    if auth_path is None:
        raise RuntimeError(
            "缺少登录状态，请先执行："
            f"uv run python main.py login --site {course.site_key}"
        )

    evidence_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = evidence_dir / f"extension-{course.site_key}-fallback.png"

    with tempfile.TemporaryDirectory(prefix="scut-extension-profile-") as profile:
        profile_dir = Path(profile)
        downloads_dir = profile_dir / "downloads"
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=False,
                accept_downloads=True,
                downloads_path=downloads_dir,
                args=[
                    f"--disable-extensions-except={EXTENSION_DIR}",
                    f"--load-extension={EXTENSION_DIR}",
                ],
            )
            try:
                browser = context.browser
                if browser is not None:
                    cdp = await browser.new_browser_cdp_session()
                    await cdp.send(
                        "Browser.setDownloadBehavior",
                        {
                            "behavior": "allow",
                            "downloadPath": str(downloads_dir),
                            "eventsEnabled": True,
                        },
                    )

                await restore_storage(context, Path(auth_path))
                course_page = await context.new_page()
                await course_page.goto(
                    course.original_url,
                    wait_until="networkidle",
                    timeout=60_000,
                )
                final_course = parse_course_url(course_page.url)
                if (
                    final_course.origin != course.origin
                    or final_course.sub_id != course.sub_id
                ):
                    raise RuntimeError(
                        f"课程页面发生了意外跳转：{course_page.url}"
                    )

                await asyncio.sleep(1)
                extension_id = await find_extension_id(profile_dir)
                popup_page = await open_popup_for_course(
                    context,
                    course_page,
                    f"chrome-extension://{extension_id}/popup.html",
                )
                await popup_page.locator("#main:not(.hidden)").wait_for(timeout=30_000)
                await popup_page.wait_for_function(
                    """() => {
                        const count = document.querySelector("#lesson-count");
                        return count && count.textContent !== "加载中...";
                    }""",
                    timeout=30_000,
                )
                await popup_page.locator("#fmt-json").check()

                fallback_count = 0

                async def abort_popup_subtitle_once(route: Route) -> None:
                    nonlocal fallback_count
                    request = route.request
                    try:
                        initiator_url = request.frame.url
                    except Exception:
                        initiator_url = ""
                    if (
                        fallback_count == 0
                        and initiator_url.startswith(
                            f"chrome-extension://{extension_id}/"
                        )
                    ):
                        fallback_count += 1
                        await route.abort("connectionfailed")
                        return
                    await route.continue_()

                await context.route(
                    f"**{SUBTITLE_API_PATH}*",
                    abort_popup_subtitle_once,
                )
                await popup_page.locator("#download-btn").click()
                await popup_page.locator("#status-single.success").wait_for(
                    timeout=60_000
                )
                status = await popup_page.locator("#status-single").inner_text()
                if fallback_count != 1:
                    raise RuntimeError(
                        "未能确认弹窗字幕请求被阻断一次，页面内回退测试无效。"
                    )

                recent_downloads: list[dict] = []
                completed_paths: list[Path] = []
                for _ in range(300):
                    recent_downloads = await get_download_items(popup_page)
                    completed_paths = [
                        Path(item["filename"]).resolve()
                        for item in recent_downloads
                        if item["state"] == "complete"
                        and Path(item["filename"]).is_file()
                    ]
                    if len(completed_paths) >= 3:
                        break
                    await asyncio.sleep(0.1)
                else:
                    states = [
                        f"{Path(item['filename']).name}: {item['state']}"
                        for item in recent_downloads
                    ]
                    raise RuntimeError(
                        "扩展显示成功，但下载记录中没有完成的 SRT/TXT/JSON "
                        f"三个文件。当前记录：{states}"
                    )

                formats = set()
                for path in completed_paths:
                    if not path.is_relative_to(downloads_dir.resolve()):
                        raise RuntimeError("测试下载文件没有落在临时目录内。")
                    content = path.read_text(encoding="utf-8")
                    try:
                        parsed_content = json.loads(content)
                    except json.JSONDecodeError:
                        parsed_content = None
                    if isinstance(parsed_content, list):
                        formats.add("JSON")
                    elif re.search(
                        r"\d{2}:\d{2}:\d{2},\d{3} --> "
                        r"\d{2}:\d{2}:\d{2},\d{3}",
                        content,
                    ):
                        formats.add("SRT")
                    elif content.strip():
                        formats.add("TXT")
                if formats != {"SRT", "TXT", "JSON"}:
                    raise RuntimeError(
                        f"三个下载文件的内容格式不完整：{sorted(formats)}"
                    )

                await popup_page.locator("body").screenshot(path=screenshot_path)
                print(f"站点：{site.origin}")
                print(f"课时：{course.sub_id}")
                print(f"页面内回退次数：{fallback_count}")
                print(f"扩展状态：{status}")
                print("下载内容：SRT、TXT、JSON 均已完成并验证")
                print(f"截图：{screenshot_path}")

                if check_batch:
                    previous_ids = {item["id"] for item in recent_downloads}
                    await popup_page.locator('.tab[data-tab="batch"]').click()
                    selected_lessons = await popup_page.locator(
                        ".lesson-check:checked"
                    ).count()
                    if selected_lessons == 0:
                        raise RuntimeError("扩展没有加载到可供批量下载的课程目录。")

                    await popup_page.locator("#batch-download-btn").click()
                    await popup_page.locator("#status-batch.success").wait_for(
                        timeout=180_000
                    )
                    batch_status = await popup_page.locator(
                        "#status-batch"
                    ).inner_text()

                    zip_path = None
                    for _ in range(300):
                        items = await get_download_items(popup_page)
                        candidates = [
                            Path(item["filename"]).resolve()
                            for item in items
                            if item["id"] not in previous_ids
                            and item["state"] == "complete"
                            and Path(item["filename"]).is_file()
                        ]
                        zip_path = next(
                            (
                                path
                                for path in candidates
                                if zipfile.is_zipfile(path)
                            ),
                            None,
                        )
                        if zip_path is not None:
                            break
                        await asyncio.sleep(0.1)
                    if zip_path is None:
                        raise RuntimeError("批量下载显示成功，但没有找到有效 ZIP。")
                    if not zip_path.is_relative_to(downloads_dir.resolve()):
                        raise RuntimeError("批量 ZIP 没有落在临时下载目录内。")

                    with zipfile.ZipFile(zip_path) as archive:
                        names = archive.namelist()
                    expected_entries = selected_lessons * 2
                    if len(names) != expected_entries:
                        raise RuntimeError(
                            f"ZIP 应有 {expected_entries} 个文件，实际为 {len(names)}。"
                        )
                    for name in names:
                        entry = PurePosixPath(name)
                        if (
                            entry.is_absolute()
                            or ".." in entry.parts
                            or "\\" in name
                            or ":" in name
                            or any(ord(character) < 32 for character in name)
                        ):
                            raise RuntimeError(f"ZIP 中存在不安全路径：{name!r}")

                    batch_screenshot = (
                        evidence_dir / f"extension-{course.site_key}-batch.png"
                    )
                    await popup_page.locator("body").screenshot(
                        path=batch_screenshot
                    )
                    print(f"批量状态：{batch_status}")
                    print(
                        f"批量 ZIP：{selected_lessons} 节，"
                        f"{len(names)} 个安全 entry"
                    )
                    print(f"批量截图：{batch_screenshot}")
                return screenshot_path
            finally:
                await context.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="实机验证未打包扩展的当前课时下载和页面内请求回退"
    )
    parser.add_argument("course_url", help="已登录站点的华园视频课程播放链接")
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_DIR,
        help="截图与临时下载的输出目录",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="同时实测整课 ZIP，并检查 ZIP entry 数量与路径安全",
    )
    args = parser.parse_args()
    asyncio.run(
        run_check(
            args.course_url,
            args.evidence_dir.resolve(),
            check_batch=args.batch,
        )
    )


if __name__ == "__main__":
    main()
