from composer.context_store import ContextStore


def test_push_and_get_counts():
    store = ContextStore()

    ok, reason, _ = store.push("merchant", "m1", 1, {"identity": {"name": "M"}})
    assert ok and reason is None

    ok2, reason2, _ = store.push("merchant", "m1", 1, {"identity": {"name": "M"}})
    assert not ok2 and reason2 == "stale_version"

    counts = store.counts()
    assert counts["merchant"] == 1

    m = store.get_merchant("m1")
    assert m and m["identity"]["name"] == "M"
