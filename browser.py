import asyncio
import json
import logging
import re
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from auth import get_auth_path, save_auth
from config import (
    CATALOGUE_API_PATH,
    LOGIN_TIMEOUT,
    SUBTITLE_API_PATH,
    SUBTITLE_TIMEOUT,
    SiteConfig,
    validate_numeric_id,
)
from subtitle import extract_subtitle_items

logger = logging.getLogger(__name__)


def _login_hint(site: SiteConfig) -> str:
    return (
        "登录状态已失效，请执行："
        f"uv run python main.py login --site {site.key}"
    )


def is_matching_subtitle_response(
    response_url: str, site: SiteConfig, expected_sub_id: str
) -> bool:
    """Accept only the exact subtitle endpoint for the current lesson."""
    try:
        parsed = urlparse(response_url)
        port = parsed.port
        expected_id = validate_numeric_id(expected_sub_id, "sub_id")
    except (ValueError, TypeError):
        return False
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or (parsed.hostname or "").lower() != site.hostname
        or port not in (None, 443)
        or parsed.path != SUBTITLE_API_PATH
    ):
        return False
    sub_ids = parse_qs(parsed.query, keep_blank_values=True).get("sub_id", [])
    return sub_ids == [expected_id]


async def create_browser(
    site_key: str, headless: bool = False
) -> tuple[Playwright, Browser, BrowserContext]:
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=headless)
    auth_path = get_auth_path(site_key)
    context = (
        await browser.new_context(storage_state=auth_path)
        if auth_path
        else await browser.new_context()
    )
    return pw, browser, context


async def close_browser(
    pw: Playwright, browser: Browser, context: BrowserContext
) -> None:
    """Close Playwright resources in dependency order (important on Windows)."""
    try:
        await context.close()
    finally:
        try:
            await browser.close()
        finally:
            await pw.stop()


async def do_login(context: BrowserContext, site: SiteConfig) -> None:
    page = await context.new_page()
    print(f"正在打开 {site.origin}")
    print("请在弹出的浏览器窗口中完成登录...")
    print(f"等待登录完成（超时 {LOGIN_TIMEOUT // 1000} 秒）")

    try:
        await page.goto(site.origin)
        await page.wait_for_url(f"{site.origin}/**", timeout=LOGIN_TIMEOUT)
        await page.wait_for_load_state("networkidle")
        if (urlparse(page.url).hostname or "").lower() != site.hostname:
            raise RuntimeError("登录后没有返回目标华园视频站点。")
        print(f"登录成功，当前地址：{page.url}")
        await save_auth(context, site.key)
        print(f"登录状态已保存到 {site.auth_state_file}")
    except Exception:
        print("登录超时或未返回目标站点，请重试。")
        raise
    finally:
        await page.close()


async def intercept_subtitle(
    context: BrowserContext,
    play_url: str,
    sub_id: str,
    site: SiteConfig,
    timeout: int = SUBTITLE_TIMEOUT,
) -> list[dict]:
    page = await context.new_page()
    subtitle_data: list[dict] | None = None
    failure_reason: str | None = None

    async def handle_response(response) -> None:
        nonlocal subtitle_data, failure_reason
        if not is_matching_subtitle_response(response.url, site, sub_id):
            return

        content_type = response.headers.get("content-type", "")
        logger.info(
            "字幕接口响应 url=%s status=%s content-type=%s",
            response.url,
            response.status,
            content_type,
        )
        if response.status in (401, 403):
            failure_reason = _login_hint(site)
            return
        if response.status != 200:
            failure_reason = f"字幕接口返回 HTTP {response.status}。"
            return
        try:
            raw_text = await response.text()
            body = json.loads(raw_text)
            items = extract_subtitle_items(body)
            if items:
                subtitle_data = items
            else:
                failure_reason = "该课时暂无字幕数据。"
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.debug("解析字幕响应失败", exc_info=True)
            failure_reason = (
                "字幕接口正文不是 JSON，可能已跳转到登录页。"
                f"（Content-Type: {content_type or '未知'}）"
                f" {_login_hint(site)}"
            )

    page.on("response", handle_response)
    logger.info("打开课程页：%s", play_url)
    try:
        await page.goto(play_url, wait_until="networkidle")
        logger.info("课程页最终地址：%s", page.url)
        if (urlparse(page.url).hostname or "").lower() != site.hostname:
            raise RuntimeError(f"页面已跳转到登录站点。{_login_hint(site)}")

        elapsed = 0
        while subtitle_data is None and failure_reason is None and elapsed < timeout / 1000:
            await asyncio.sleep(1)
            elapsed += 1
    finally:
        await page.close()

    if subtitle_data is not None:
        return subtitle_data
    if failure_reason:
        raise RuntimeError(failure_reason)
    raise RuntimeError(
        f"未能获取课程 {sub_id} 的字幕数据（超时 {timeout // 1000}s），"
        "请确认课程有字幕且登录状态有效。"
    )


async def get_course_list(context: BrowserContext, site: SiteConfig) -> list[dict]:
    page = await context.new_page()
    courses: list[dict] = []

    async def handle_response(response) -> None:
        if response.status != 200:
            return
        if (urlparse(response.url).hostname or "").lower() != site.hostname:
            return
        if "courseapi" not in response.url and "courselist" not in response.url:
            return
        try:
            body = await response.json()
            if isinstance(body, list):
                courses.extend(body)
            elif isinstance(body, dict):
                for key in ("data", "result", "list", "rows", "courses"):
                    if isinstance(body.get(key), list):
                        courses.extend(body[key])
                        break
        except Exception:
            logger.debug("解析课程列表响应失败", exc_info=True)

    page.on("response", handle_response)
    await page.goto(site.origin, wait_until="networkidle")
    await asyncio.sleep(3)
    await page.close()

    if not courses:
        page = await context.new_page()
        await page.goto(site.origin, wait_until="networkidle")
        items = await page.query_selector_all("a[href*='sub_id'], a[href*='course_id']")
        for item in items:
            href = await item.get_attribute("href") or ""
            text = (await item.inner_text()).strip()
            sub_match = re.search(r"sub_id=(\d+)", href)
            course_match = re.search(r"course_id=(\d+)", href)
            if sub_match:
                entry = {"sub_id": sub_match.group(1), "name": text}
                if course_match:
                    entry["course_id"] = course_match.group(1)
                courses.append(entry)
        await page.close()
    return courses


async def get_course_catalogue(
    context: BrowserContext, site: SiteConfig, course_id: str
) -> list[dict]:
    safe_course_id = validate_numeric_id(course_id, "course_id")
    page = await context.new_page()
    try:
        await page.goto(site.origin, wait_until="networkidle")
        if (urlparse(page.url).hostname or "").lower() != site.hostname:
            raise RuntimeError(f"页面已跳转到登录站点。{_login_hint(site)}")
        result = await page.evaluate(
            """async ({path, courseId}) => {
                const url = new URL(path, location.origin);
                url.searchParams.set("course_id", courseId);
                const resp = await fetch(url, {credentials: "include"});
                const contentType = resp.headers.get("content-type") || "";
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                if (!contentType.toLowerCase().includes("json")) {
                    throw new Error("接口未返回 JSON，可能需要重新登录");
                }
                return {url: url.href, status: resp.status, body: await resp.json()};
            }""",
            {"path": CATALOGUE_API_PATH, "courseId": safe_course_id},
        )
        logger.info("课程目录接口：%s status=%s", result["url"], result["status"])
        return result["body"].get("result", {}).get("data", [])
    except Exception as exc:
        raise RuntimeError(f"课程目录获取失败：{exc}") from exc
    finally:
        await page.close()
