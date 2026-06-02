"""Unit tests for the Facebook Page tools (posts engagement + page insights)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def mock_settings():
    settings = MagicMock()
    settings.instagram_api_url = "https://graph.facebook.com/v22.0"
    settings.instagram_access_token = "user_token"
    settings.rate_limit_requests_per_hour = 200
    settings.cache_enabled = False
    settings.cache_ttl_seconds = 300
    with patch("src.instagram_client.get_settings", return_value=settings):
        yield settings


from src.instagram_client import InstagramClient  # noqa: E402
from src.models.instagram_models import AccountInsight, FacebookPage  # noqa: E402


async def test_get_page_access_token_caches():
    c = InstagramClient()
    c._make_request = AsyncMock(return_value={"access_token": "PAGE_TOK"})
    assert await c._get_page_access_token("PAGE1") == "PAGE_TOK"
    assert await c._get_page_access_token("PAGE1") == "PAGE_TOK"
    c._make_request.assert_awaited_once()  # second call served from cache


async def test_get_page_access_token_missing_raises():
    c = InstagramClient()
    c._make_request = AsyncMock(return_value={})
    with pytest.raises(Exception):
        await c._get_page_access_token("PAGE1")


async def test_get_facebook_posts_flattens_engagement():
    c = InstagramClient()
    c._page_tokens = {"PAGE1": "PAGE_TOK"}
    c._make_request = AsyncMock(
        return_value={
            "data": [
                {
                    "id": "PAGE1_1",
                    "created_time": "2026-05-29T00:36:32+0000",
                    "message": "hello",
                    "permalink_url": "https://fb/1",
                    "status_type": "added_photos",
                    "reactions": {"summary": {"total_count": 9}},
                    "comments": {"summary": {"total_count": 1}},
                    "shares": {"count": 3},
                },
                {"id": "PAGE1_2"},  # no engagement fields
            ]
        }
    )
    posts = await c.get_facebook_posts(page_id="PAGE1", limit=10)
    assert (
        posts[0].reactions_count,
        posts[0].comments_count,
        posts[0].shares_count,
    ) == (
        9,
        1,
        3,
    )
    assert posts[1].shares_count == 0  # missing shares -> 0
    assert posts[1].reactions_count is None

    call = c._make_request.await_args
    assert call.args[1] == "PAGE1/posts"
    assert call.kwargs["params"]["access_token"] == "PAGE_TOK"
    assert "reactions.summary" in call.kwargs["params"]["fields"]


async def test_get_facebook_posts_defaults_page_id():
    c = InstagramClient()

    async def fake_pages():
        return [FacebookPage(id="PAGEX", name="X")]

    c.get_account_pages = fake_pages
    c._make_request = AsyncMock(
        side_effect=[{"access_token": "TOKX"}, {"data": []}]  # token, then posts
    )
    assert await c.get_facebook_posts() == []
    assert c._make_request.await_args_list[0].args[1] == "PAGEX"  # token resolved


async def test_get_facebook_page_insights_builds_params():
    c = InstagramClient()
    c._page_tokens = {"PAGE1": "PAGE_TOK"}
    c._make_request = AsyncMock(return_value={"data": []})
    await c.get_facebook_page_insights(page_id="PAGE1")
    p = c._make_request.await_args.kwargs["params"]
    assert c._make_request.await_args.args[1] == "PAGE1/insights"
    assert p["metric_type"] == "total_value"
    assert p["access_token"] == "PAGE_TOK"
    assert "page_post_engagements" in p["metric"]


async def test_page_insights_tool_adds_note_when_empty():
    import src.instagram_mcp_server as s

    body = s.get_facebook_page_insights.__wrapped__

    class EmptyClient:
        async def get_facebook_page_insights(self, *a, **k):
            return []

    s.instagram_client = EmptyClient()
    assert "note" in await body()

    class DataClient:
        async def get_facebook_page_insights(self, *a, **k):
            return [
                AccountInsight(
                    name="page_views_total", period="day", values=[{"value": 5}]
                )
            ]

    s.instagram_client = DataClient()
    assert "note" not in await body()
