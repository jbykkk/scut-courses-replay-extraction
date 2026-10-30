import argparse
import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from audio import (
    ReplayError,
    download_audio,
    extract_media,
    output_paths,
    read_report,
    require_binary,
    update_manifest,
)
from auth import is_logged_in
from browser import (
    close_browser,
    create_browser,
    do_login,
    get_course_catalogue,
    get_replay_info,
    intercept_subtitle,
)
from config import (
    CourseUrl,
    CourseUrlError,
    OUTPUT_DIR,
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


def audio_request(args) -> tuple[str, str, list[str]]:
    """Resolve URL/report/explicit IDs before opening a browser or writing files."""
    course_ids = []
    sub_ids = list(args.sub_id or [])
    site_key = args.site
    if args.course_id is not None:
        course_ids.append(validate_numeric_id(args.course_id, "course_id"))
    if args.url:
        course = parse_course_url(args.url)
        if site_key is not None and site_key != course.site_key:
            raise ReplayError("--site 与课程链接所属站点不一致。")
        site_key = course.site_key
        if course.course_id:
            course_ids.append(course.course_id)
        sub_ids.append(course.sub_id)
    if args.from_report:
        course_id, report_ids = read_report(args.from_report, args.include_errors)
        course_ids.append(course_id)
        sub_ids.extend(report_ids)
    elif args.include_errors:
        raise ReplayError("--include-errors 需要配合 --from-report 使用。")
    if not course_ids:
        raise ReplayError("缺少 course_id，请提供课程链接、--course-id 或 --from-report。")
    if len(set(course_ids)) != 1:
        raise ReplayError("输入的 course_id 不一致，请每次处理同一门课程。")
    sub_ids = list(dict.fromkeys(validate_numeric_id(sid, "sub_id") for sid in sub_ids))
    if not sub_ids and not args.from_report:
        raise ReplayError("请提供课程链接、--sub-id 或 --from-report。")
    return site_key or "internal", course_ids[0], sub_ids


async def cmd_audio(args) -> bool:
    site_key, course_id, sub_ids = audio_request(args)
    if not sub_ids:
        print("报告中没有需要下载音频的课时。")
        return True
    site = get_site(site_key)
    if not is_logged_in(site_key):
        raise ReplayError(
            f"{site.origin} 尚未登录，请先执行：uv run python main.py login --site {site_key}"
        )
    ffmpeg = require_binary(args.ffmpeg) if not args.dry_run else None
    ffprobe = require_binary("ffprobe") if not args.dry_run else None
    pw, browser, context = await create_browser(site_key, headless=not args.headful)
    downloaded = skipped = probed = failed = 0
    try:
        for index, sub_id in enumerate(sub_ids, 1):
            print(f"[{index}/{len(sub_ids)}] sub_id={sub_id}", flush=True)
            try:
                data = await get_replay_info(context, site, course_id, sub_id)
                field, url = extract_media(data)
                output, manifest = output_paths(
                    args.out_dir, course_id, sub_id, data.get("sub_title")
                )
                print(f"  课时：{data.get('sub_title') or sub_id}")
                print(
                    f"  状态：sub_status={data.get('sub_status')} "
                    f"is_m3u8={data.get('is_m3u8')}"
                )
                print(f"  媒体：{field} -> {url}")
                if args.dry_run:
                    print(f"  计划输出：{output}（dry-run）")
                    probed += 1
                    continue
                print("  正在提取音轨…", flush=True)
                status, metadata = await download_audio(
                    ffmpeg, ffprobe, url, output, args.force
                )
                now = datetime.now().astimezone().isoformat(timespec="seconds")
                entry = {
                    "course_id": course_id,
                    "course_title": data.get("course_title"),
                    "sub_id": sub_id,
                    "title": data.get("sub_title"),
                    "site": site_key,
                    "media_url": url,
                    "source_field": field,
                    "audio_file": str(output),
                    "checked_at": now,
                    **metadata,
                }
                if status == "downloaded":
                    entry["downloaded_at"] = now
                update_manifest(manifest, entry)
                downloaded += status == "downloaded"
                skipped += status == "exists"
                label = "已下载" if status == "downloaded" else "已存在，跳过"
                print(f"  {label}：{output}（{metadata['duration_seconds']:.1f}s）")
            except Exception as exc:
                logging.getLogger(__name__).debug("音频处理失败", exc_info=True)
                print(f"  失败：{exc}")
                failed += 1
                if args.stop_on_error:
                    break
    finally:
        await close_browser(pw, browser, context)
    print(f"\n完成：下载 {downloaded}，跳过 {skipped}，探测 {probed}，失败 {failed}")
    return failed == 0


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
    elif args.command == "audio":
        try:
            success = asyncio.run(cmd_audio(args))
        except Exception as exc:
            logging.getLogger(__name__).debug("音频任务失败", exc_info=True)
            print(f"音频任务失败：{exc}")
            success = False
        if not success:
            raise SystemExit(1)
    else:
        parser.print_help()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="华园视频校内/校外字幕与回放音频提取工具")
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

    audio_parser = subparsers.add_parser("audio", help="下载回放音频，供后续转写使用")
    audio_parser.add_argument("url", nargs="?", help="课程播放链接，自动识别校内/校外站点")
    audio_parser.add_argument("--course-id", help="课程 ID，也可从链接或报告读取")
    audio_parser.add_argument("--sub-id", nargs="+", help="课时 ID，支持多个")
    audio_parser.add_argument("--from-report", type=Path, help="从字幕导出结果.json 选择暂无字幕的课时")
    audio_parser.add_argument("--include-errors", action="store_true", help="同时处理报告中请求失败的课时")
    audio_parser.add_argument("--site", choices=tuple(SITES), help="无链接时默认 internal；WebVPN 用 external")
    audio_parser.add_argument(
        "--out-dir", type=Path, default=OUTPUT_DIR / "audio",
        help="音频根目录（默认 output/audio，按 course_id 分目录）",
    )
    audio_parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 可执行文件路径")
    audio_parser.add_argument("--dry-run", action="store_true", help="只探测媒体地址，不下载或写清单")
    audio_parser.add_argument("--force", action="store_true", help="重新下载并替换已有音频")
    audio_parser.add_argument("--stop-on-error", action="store_true", help="遇到首个失败停止批量任务")
    audio_parser.add_argument("--headful", action="store_true", help="显示浏览器窗口以排查登录问题")

    return parser


if __name__ == "__main__":
    main()
