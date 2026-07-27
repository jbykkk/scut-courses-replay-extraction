import argparse
import asyncio
import logging
from collections import defaultdict

from auth import is_logged_in
from browser import (
    close_browser,
    create_browser,
    do_login,
    get_course_catalogue,
    intercept_subtitle,
)
from config import (
    CourseUrl,
    CourseUrlError,
    SITES,
    build_livingroom_url,
    get_site,
    parse_course_url,
    validate_numeric_id,
)
from subtitle import save_subtitle


def _read_course_url(prompt: str) -> str:
    print(prompt)
    return input("> ").strip()


def _parse_or_report(url: str) -> CourseUrl | None:
    try:
        return parse_course_url(url)
    except CourseUrlError as exc:
        print(f"无法解析链接：{exc}\n  {url}")
        return None


async def cmd_login(args) -> bool:
    site = get_site(args.site)
    pw, browser, context = await create_browser(site.key, headless=False)
    try:
        await do_login(context, site)
        return True
    except Exception:
        logging.getLogger(__name__).debug("登录失败", exc_info=True)
        return False
    finally:
        await close_browser(pw, browser, context)


async def _download_site_tasks(
    site_key: str,
    tasks: list[tuple[CourseUrl, str]],
    headless: bool,
) -> tuple[int, int]:
    site = get_site(site_key)
    if not is_logged_in(site_key):
        print(
            f"{site.origin} 尚未登录，请先执行："
            f"uv run python main.py login --site {site_key}"
        )
        return 0, len(tasks)

    pw, browser, context = await create_browser(site_key, headless=headless)
    success = 0
    failed = 0
    try:
        for index, (course, title) in enumerate(tasks, 1):
            label = f"{title} ({course.sub_id})" if title else course.sub_id
            print(f"[{index}/{len(tasks)}] {label}")
            try:
                data = await intercept_subtitle(
                    context,
                    play_url=course.original_url,
                    sub_id=course.sub_id,
                    site=site,
                )
                save_subtitle(data, course.sub_id, course_name=title or None)
                print(f"  成功！共 {len(data)} 条字幕")
                success += 1
            except Exception as exc:
                print(f"  失败：{exc}")
                failed += 1
    finally:
        await close_browser(pw, browser, context)
    return success, failed


async def cmd_get(args) -> None:
    raw_urls = list(args.urls)
    if not raw_urls:
        prompt = "请粘贴课程链接以获取课程目录：" if args.all else "请粘贴课程链接："
        entered = _read_course_url(prompt)
        if not entered:
            print("未提供链接。")
            return
        raw_urls.append(entered)

    parsed_urls = [course for url in raw_urls if (course := _parse_or_report(url))]
    if not parsed_urls:
        return

    tasks_by_site: dict[str, list[tuple[CourseUrl, str]]] = defaultdict(list)

    if args.all:
        if len(parsed_urls) != 1:
            print("--all 每次只接受一个课程链接。")
            return
        source = parsed_urls[0]
        if not source.course_id:
            print("链接中缺少 course_id，无法获取整门课程目录。")
            return
        if not is_logged_in(source.site_key):
            print(
                "对应站点尚未登录，请先执行："
                f"uv run python main.py login --site {source.site_key}"
            )
            return

        site = get_site(source.site_key)
        print(f"正在从 {site.origin} 获取课程目录（course_id={source.course_id}）...")
        pw, browser, context = await create_browser(source.site_key, headless=args.headless)
        try:
            catalogue = await get_course_catalogue(context, site, source.course_id)
        except Exception as exc:
            print(exc)
            return
        finally:
            await close_browser(pw, browser, context)

        if not catalogue:
            print("未找到课程目录。")
            return
        print(f"共 {len(catalogue)} 节课\n")
        for lesson in catalogue:
            try:
                sub_id = validate_numeric_id(lesson.get("sub_id"), "sub_id")
            except ValueError:
                logging.warning("跳过包含非法 sub_id 的课程目录项：%r", lesson)
                continue
            play_url = build_livingroom_url(
                source.origin,
                sub_id,
                course_id=source.course_id,
                tenant_code=source.tenant_code,
            )
            course = CourseUrl(
                site_key=source.site_key,
                origin=source.origin,
                original_url=play_url,
                course_id=source.course_id,
                sub_id=sub_id,
                tenant_code=source.tenant_code,
            )
            tasks_by_site[source.site_key].append((course, lesson.get("title", "")))
    else:
        for course in parsed_urls:
            tasks_by_site[course.site_key].append((course, ""))

    total_success = 0
    total_failed = 0
    for site_key, tasks in tasks_by_site.items():
        success, failed = await _download_site_tasks(site_key, tasks, args.headless)
        total_success += success
        total_failed += failed
    print(f"\n完成：成功 {total_success}，失败 {total_failed}")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.command == "login":
        if not asyncio.run(cmd_login(args)):
            raise SystemExit(1)
    elif args.command == "get":
        asyncio.run(cmd_get(args))
    else:
        parser.print_help()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="华园视频校内/校外字幕提取工具")
    parser.add_argument("--verbose", action="store_true", help="显示接口诊断日志")
    subparsers = parser.add_subparsers(dest="command")

    login_parser = subparsers.add_parser("login", help="登录华园视频")
    login_parser.add_argument(
        "--site",
        choices=tuple(SITES),
        default="internal",
        help="登录站点：internal=校内（默认），external=WebVPN",
    )

    get_parser = subparsers.add_parser("get", help="获取课程字幕")
    get_parser.add_argument("urls", nargs="*", help="课程链接（支持多个）")
    get_parser.add_argument("--all", action="store_true", help="批量导出整门课程字幕")
    get_parser.add_argument("--headless", action="store_true", help="不显示浏览器窗口")

    return parser


if __name__ == "__main__":
    main()
