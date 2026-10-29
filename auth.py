from config import DATA_DIR, LEGACY_AUTH_STATE_FILE, get_site


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def is_logged_in(site_key: str) -> bool:
    return get_auth_path(site_key) is not None


async def save_auth(context, site_key: str) -> None:
    ensure_data_dir()
    await context.storage_state(path=str(get_site(site_key).auth_state_file))


def get_auth_path(site_key: str) -> str | None:
    auth_file = get_site(site_key).auth_state_file
    if auth_file.exists() and auth_file.stat().st_size > 0:
        return str(auth_file)
    if (
        site_key == "internal"
        and LEGACY_AUTH_STATE_FILE.exists()
        and LEGACY_AUTH_STATE_FILE.stat().st_size > 0
    ):
        return str(LEGACY_AUTH_STATE_FILE)
    return None
