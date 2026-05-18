from __future__ import annotations

from sqlalchemy.orm import Session

from app.common.time import utc_now
from app.db.models.app_setting import AppSetting


def get_setting(session: Session, key: str) -> AppSetting | None:
    return session.get(AppSetting, key)


def list_settings(session: Session, keys: list[str] | None = None) -> dict[str, str]:
    query = session.query(AppSetting)
    if keys is not None:
        query = query.filter(AppSetting.key.in_(keys))
    return {setting.key: setting.value for setting in query.all()}


def set_setting(session: Session, key: str, value: str) -> AppSetting:
    setting = session.get(AppSetting, key)
    if setting is None:
        setting = AppSetting(key=key, value=value, updated_at=utc_now())
        session.add(setting)
    else:
        setting.value = value
        setting.updated_at = utc_now()
    session.flush()
    return setting


def delete_setting(session: Session, key: str) -> None:
    setting = session.get(AppSetting, key)
    if setting is not None:
        session.delete(setting)
        session.flush()
