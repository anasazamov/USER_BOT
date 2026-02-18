from app.priority_groups import parse_priority_group_link


def test_parse_priority_group_public_username_link() -> None:
    parsed = parse_priority_group_link("https://t.me/Qoqon_Surxandaryo")
    assert parsed is not None
    assert parsed.username == "qoqon_surxandaryo"
    assert parsed.invite_link is None


def test_parse_priority_group_raw_tme_username_link() -> None:
    parsed = parse_priority_group_link("t.me/Toshkent_samarqan_taksi")
    assert parsed is not None
    assert parsed.username == "toshkent_samarqan_taksi"
    assert parsed.invite_link is None


def test_parse_priority_group_treats_hash_like_value_as_invite() -> None:
    parsed = parse_priority_group_link("t.me/Ele1JGFwZDc1MWVi")
    assert parsed is not None
    assert parsed.invite_link == "https://t.me/+Ele1JGFwZDc1MWVi"
    assert parsed.username is None
