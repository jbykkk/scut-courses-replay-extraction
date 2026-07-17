from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"


@dataclass(frozen=True)
class SiteConfig:
    key: str
    origin: str
    hostname: str
    auth_state_name: str

    @property
    def auth_state_file(self) -> Path:
        return DATA_DIR / self.auth_state_name


@dataclass(frozen=True)
class CourseUrl:
    site_key: str
    origin: str
    original_url: str
    course_id: str | None
    sub_id: str
    tenant_code: str


class CourseUrlError(ValueError):
    """Raised when a URL is not a supported Huayuan Video lesson URL."""


SITES = {
    "internal": SiteConfig(
        key="internal",
        origin="https://video.jw.scut.edu.cn",
        hostname="video.jw.scut.edu.cn",
        auth_state_name="auth_state_internal.json",
    ),
    "external": SiteConfig(
        key="external",
        origin="https://video-jw-443.webvpn.scut.edu.cn",
        hostname="video-jw-443.webvpn.scut.edu.cn",
        auth_state_name="auth_state_external.json",
    ),
}
SITE_BY_HOSTNAME = {site.hostname: site for site in SITES.values()}

SUBTITLE_API_PATH = "/courseapi/v3/web-socket/search-trans-result"
CATALOGUE_API_PATH = "/courseapi/v2/course/catalogue"
SUBTITLE_URL_PATTERN = "search-trans-result"

LOGIN_TIMEOUT = 120_000
SUBTITLE_TIMEOUT = 60_000


def get_site(site_key: str) -> SiteConfig:
    try:
        return SITES[site_key]
    except KeyError as exc:
        raise ValueError(f"未知站点：{site_key}") from exc


def parse_course_url(url: str) -> CourseUrl:
    """Parse and validate an internal or WebVPN lesson URL."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise CourseUrlError("课程链接格式不正确。") from exc

    if parsed.scheme != "https":
        raise CourseUrlError("课程链接必须使用 HTTPS。")

    hostname = (parsed.hostname or "").lower()
    site = SITE_BY_HOSTNAME.get(hostname)
    if site is None:
        raise CourseUrlError("链接不是受支持的华园视频校内或校外网址。")

    if parsed.path.rstrip("/") != "/livingroom":
        raise CourseUrlError("链接不是华园视频课程播放页（/livingroom）。")

    query = parse_qs(parsed.query)
    sub_id = query.get("sub_id", [None])[0]
    if not sub_id:
        raise CourseUrlError("课程链接中缺少 sub_id。")

    course_id = query.get("course_id", [None])[0]
    tenant_code = query.get("tenant_code", ["21"])[0] or "21"
    return CourseUrl(
        site_key=site.key,
        origin=site.origin,
        original_url=url,
        course_id=course_id,
        sub_id=sub_id,
        tenant_code=tenant_code,
    )


def build_livingroom_url(
    origin: str,
    sub_id: str,
    course_id: str | None = None,
    tenant_code: str = "21",
) -> str:
    params = {"sub_id": sub_id, "tenant_code": tenant_code}
    if course_id:
        params = {"course_id": course_id, **params}
    return f"{origin}/livingroom?{urlencode(params)}"
