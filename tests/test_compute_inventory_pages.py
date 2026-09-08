from dataclasses import replace
import pytest

from tests.test_compute_index import inventory, NOW


@pytest.mark.parametrize("size", [16, 64, 256])
@pytest.mark.parametrize("limit", [1, 17, 32, 64])
def test_inventory_pages_traverse_stable_membership_with_live_observations(size, limit, monkeypatch):
    registry, snapshots = inventory(size)
    monkeypatch.setattr(registry, "configured_nodes", lambda: pytest.fail("full configured materialization"))
    monkeypatch.setattr(registry, "list_snapshots", lambda **kwargs: pytest.fail("full observation materialization"))
    seen, cursor = [], None
    while True:
        page = registry.inventory_page(now=NOW, limit=limit, cursor=cursor)
        assert len(page["nodes"]) <= limit
        seen.extend(row["node_id"] for row in page["nodes"])
        assert page["has_more"] == (len(seen) < size)
        registry.observe(replace(snapshots[-1], active_jobs=len(seen)), received_at=NOW)
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
    assert seen == [f"node-{i:03}" for i in range(size)]


def test_inventory_cursor_rejects_generation_and_malformed_values():
    registry, _ = inventory(16)
    other, _ = inventory(16)
    cursor = registry.inventory_page(now=NOW, limit=1)["next_cursor"]
    for value in ("garbage", "x" * 4097, cursor):
        with pytest.raises(ValueError):
            other.inventory_page(now=NOW, cursor=value)
    for limit in (True, 0, 65, "2"):
        with pytest.raises(ValueError):
            registry.inventory_page(now=NOW, limit=limit)
