from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import parse_qs, urlencode, urlparse


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
LEGACY_AUTH_STATE_FILE = DATA_DIR / "auth_state.json"
NUMERIC_ID_RE = re.compile(r"^[0-9]+$")


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

LOGIN_TIMEOUT = 120_000
SUBTITLE_TIMEOUT = 60_000


def get_site(site_key: str) -> SiteConfig:
    try:
        return SITES[site_key]
    except KeyError as exc:
        raise ValueError(f"未知站点：{site_key}") from exc


def validate_numeric_id(value: object, field_name: str) -> str:
    """Return a canonical decimal ID or reject it."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是纯数字。")
    normalized = str(value)
    if not NUMERIC_ID_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} 必须是纯数字。")
    return normalized


def _query_id(
    query: dict[str, list[str]],
    field_name: str,
    *,
    required: bool,
    default: str | None = None,
) -> str | None:
    values = query.get(field_name)
    if values is None:
        if required:
            raise CourseUrlError(f"课程链接中缺少 {field_name}。")
        return default
    if len(values) != 1:
        raise CourseUrlError(f"课程链接中的 {field_name} 不能重复。")
    try:
        return validate_numeric_id(values[0], field_name)
    except ValueError as exc:
        raise CourseUrlError(str(exc)) from exc


def parse_course_url(url: str) -> CourseUrl:
    """Parse and validate an internal or WebVPN lesson URL."""
    try:
        parsed = urlparse(url)
    except (TypeError, ValueError) as exc:
        raise CourseUrlError("课程链接格式不正确。") from exc

    if parsed.scheme != "https":
        raise CourseUrlError("课程链接必须使用 HTTPS。")
    if parsed.username is not None or parsed.password is not None:
        raise CourseUrlError("课程链接不能包含用户名或密码。")

    try:
        port = parsed.port
    except ValueError as exc:
        raise CourseUrlError("课程链接端口格式不正确。") from exc
    if port not in (None, 443):
        raise CourseUrlError("课程链接只能使用标准 HTTPS 端口 443。")

    hostname = (parsed.hostname or "").lower()
    site = SITE_BY_HOSTNAME.get(hostname)
    if site is None:
        raise CourseUrlError("链接不是受支持的华园视频校内或校外网址。")

    if parsed.path not in ("/livingroom", "/livingroom/"):
        raise CourseUrlError("链接不是华园视频课程播放页（/livingroom）。")

    query = parse_qs(parsed.query, keep_blank_values=True)
    sub_id = _query_id(query, "sub_id", required=True)
    course_id = _query_id(query, "course_id", required=False)
    tenant_code = _query_id(query, "tenant_code", required=False, default="21")
    assert sub_id is not None
    assert tenant_code is not None
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
    if origin not in {site.origin for site in SITES.values()}:
        raise ValueError("不支持的华园视频站点 origin。")
    safe_sub_id = validate_numeric_id(sub_id, "sub_id")
    safe_tenant_code = validate_numeric_id(tenant_code, "tenant_code")
    params = {"sub_id": safe_sub_id, "tenant_code": safe_tenant_code}
    if course_id is not None:
        params = {
            "course_id": validate_numeric_id(course_id, "course_id"),
            **params,
        }
    return f"{origin}/livingroom?{urlencode(params)}"
