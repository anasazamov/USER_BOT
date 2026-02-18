from app.geo import GeoResolver


def test_geo_detects_region_from_city_typo() -> None:
    resolver = GeoResolver()
    match = resolver.detect_region("samrqanddan toshkentga taxi kerak")
    assert match is not None
    assert match.hashtag in {"#SamarqandViloyati", "#ToshkentShahri", "#ToshkentViloyati"}


def test_geo_detects_karakalpak_region() -> None:
    resolver = GeoResolver()
    match = resolver.detect_region("nukusdan xivaga yuradigan moshin bormi")
    assert match is not None
    assert match.hashtag == "#Qoraqalpogiston"


def test_geo_detects_samarqand_aliases() -> None:
    resolver = GeoResolver()
    match = resolver.detect_region("jartepadan marhoboga 1 kishi bor")
    assert match is not None
    assert match.hashtag == "#SamarqandViloyati"


def test_geo_detects_toshkent_typo_alias() -> None:
    resolver = GeoResolver()
    match = resolver.detect_region("toshkintdan urgutga ketadigan kim bor")
    assert match is not None
    assert match.hashtag in {"#ToshkentShahri", "#ToshkentViloyati"}
