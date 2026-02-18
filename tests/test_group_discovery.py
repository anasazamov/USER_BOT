from app.group_discovery import GroupDiscoveryManager


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
