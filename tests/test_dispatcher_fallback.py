import asyncio

from composer.context_store import ContextStore
from composer.dispatcher import Dispatcher


async def _dummy_llm(system, user):
    raise RuntimeError("no llm")


def make_basic_store():
    store = ContextStore()
    # category
    store.push("category", "restaurants", 1, {"slug": "restaurants", "voice": {}})
    # merchant
    store.push(
        "merchant",
        "m1",
        1,
        {"identity": {"owner_first_name": "Alex", "name": "Alex's Diner"}, "category_slug": "restaurants"},
    )
    # trigger
    store.push(
        "trigger",
        "t1",
        1,
        {"merchant_id": "m1", "kind": "perf_spike", "payload": {"metric": "views", "delta_pct": 42}},
    )
    return store


def test_dispatcher_fallback_returns_action_with_validation_issues():
    store = make_basic_store()
    dispatcher = Dispatcher(store, _dummy_llm)

    action = asyncio.run(dispatcher.compose_for_trigger("t1"))
    assert action is not None
    assert "body" in action and isinstance(action["body"], str)
    assert "validation_issues" in action and isinstance(action["validation_issues"], list)
