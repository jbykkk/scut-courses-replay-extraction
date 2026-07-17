from config import DATA_DIR, get_site


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def is_logged_in(site_key: str) -> bool:
    auth_file = get_site(site_key).auth_state_file
    return auth_file.exists() and auth_file.stat().st_size > 0


async def save_auth(context, site_key: str) -> None:
    ensure_data_dir()
    await context.storage_state(path=str(get_site(site_key).auth_state_file))


def get_auth_path(site_key: str) -> str | None:
    if is_logged_in(site_key):
        return str(get_site(site_key).auth_state_file)
    return None
