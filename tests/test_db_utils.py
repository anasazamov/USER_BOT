from app.storage.db import (
    _extract_database_name_from_dsn,
    _is_safe_database_name,
    _replace_database_name_in_dsn,
)


def test_extract_database_name_from_dsn() -> None:
    dsn = "postgresql://postgres:postgres@localhost:5432/userbot?sslmode=disable"
    assert _extract_database_name_from_dsn(dsn) == "userbot"


def test_replace_database_name_in_dsn_keeps_query() -> None:
    dsn = "postgresql://postgres:postgres@db:5432/userbot?sslmode=disable"
    updated = _replace_database_name_in_dsn(dsn, "postgres")
    assert updated == "postgresql://postgres:postgres@db:5432/postgres?sslmode=disable"


def test_safe_database_name_validation() -> None:
    assert _is_safe_database_name("userbot")
    assert _is_safe_database_name("user_bot_2026")
    assert not _is_safe_database_name("user-bot")
    assert not _is_safe_database_name("1userbot")
