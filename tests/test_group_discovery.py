from types import SimpleNamespace

from app.geo import GeoResolver
from app.group_discovery import GroupDiscoveryManager


def _chat(title: str, username: str = "", participants: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(title=title, username=username, participants_count=participants)


def test_prioritize_queries_moves_target_regions_first() -> None:
    queries = (
        "taxi navoiy",
        "taxi toshkent",
        "taxi samarqand",
        "taxi buxoro",
    )
    prioritized = GroupDiscoveryManager._prioritize_queries(queries)
    assert prioritized[0] in {"taxi toshkent", "taxi samarqand"}
    assert prioritized[1] in {"taxi toshkent", "taxi samarqand"}


def test_is_taxi_relevant_accepts_direct_taxi_keyword() -> None:
    geo = GeoResolver()
    chat = _chat("Toshkent Taksi Guruhi", "tos_taksi")
    relevant, reason = GroupDiscoveryManager._is_taxi_relevant(chat, geo)
    assert relevant
    assert reason.startswith("taxi_keyword:")


def test_is_taxi_relevant_accepts_route_pair_without_taxi_word() -> None:
    geo = GeoResolver()
    # Real example from PRIORITY_GROUP_LINKS: Urgut (Samarqand region) + Toshkent.
    chat = _chat("urgut toshkent", "urguttoshkint")
    relevant, reason = GroupDiscoveryManager._is_taxi_relevant(chat, geo)
    assert relevant
    assert "route_pair" in reason


def test_is_taxi_relevant_rejects_single_region_no_taxi_word() -> None:
    geo = GeoResolver()
    chat = _chat("Toshkent Forum", "tashkent_forum")
    relevant, _ = GroupDiscoveryManager._is_taxi_relevant(chat, geo)
    assert not relevant


def test_is_taxi_relevant_rejects_blacklisted_topic() -> None:
    geo = GeoResolver()
    chat = _chat("Crypto Trading Signals", "crypto_signals")
    relevant, reason = GroupDiscoveryManager._is_taxi_relevant(chat, geo)
    assert not relevant
    assert reason.startswith("blacklist:")


def test_is_taxi_relevant_blacklist_wins_over_taxi_keyword() -> None:
    geo = GeoResolver()
    # A "taxi news" channel should still be rejected — taxi is in the keyword list
    # but news/yangilik puts it in the wrong bucket.
    chat = _chat("Taxi yangilik kanali", "taxi_news_uz")
    relevant, reason = GroupDiscoveryManager._is_taxi_relevant(chat, geo)
    assert not relevant
    assert reason.startswith("blacklist:")


def test_is_taxi_relevant_unrelated_title_rejected() -> None:
    geo = GeoResolver()
    chat = _chat("Sevimli mavzular", "lovely_topics")
    relevant, _ = GroupDiscoveryManager._is_taxi_relevant(chat, geo)
    assert not relevant


def test_is_size_appropriate_lenient_when_unknown() -> None:
    chat = _chat("Anything", participants=None)
    ok, reason = GroupDiscoveryManager._is_size_appropriate(chat)
    assert ok
    assert reason == "size_unknown"


def test_is_size_appropriate_rejects_too_small() -> None:
    chat = _chat("Anything", participants=5)
    ok, reason = GroupDiscoveryManager._is_size_appropriate(chat)
    assert not ok
    assert reason.startswith("too_small:")


def test_is_size_appropriate_rejects_too_large() -> None:
    chat = _chat("Anything", participants=500_000)
    ok, reason = GroupDiscoveryManager._is_size_appropriate(chat)
    assert not ok
    assert reason.startswith("too_large:")


def test_is_size_appropriate_accepts_within_window() -> None:
    chat = _chat("Anything", participants=5000)
    ok, reason = GroupDiscoveryManager._is_size_appropriate(chat)
    assert ok
    assert reason.startswith("size_ok:")
