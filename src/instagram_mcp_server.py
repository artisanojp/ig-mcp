#!/usr/bin/env python3
"""
Instagram MCP Server - A Model Context Protocol server for Instagram API integration.

This server provides tools, resources, and prompts for interacting with Instagram's Graph API,
enabling AI applications to manage Instagram business accounts programmatically.

It runs over the MCP Streamable HTTP transport so it can be reached remotely, and (when
``AUTH_ENABLED``) acts as an OAuth 2.1 Resource Server that validates Microsoft Entra ID
(Microsoft 365) bearer tokens before any tool runs. See ``src/auth.py``.
"""

import asyncio
import functools
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import structlog
from mcp.server.fastmcp import FastMCP
from mcp.types import Icon
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from .auth import IGMcpAuthProvider, build_auth_settings
from .config import get_settings
from .instagram_client import InstagramAPIError, InstagramClient
from .models.instagram_models import (
    InsightMetric,
    InsightPeriod,
    MCPToolResult,
    PublishMediaRequest,
    SendDMRequest,
)

# Configure logging
logger = structlog.get_logger(__name__)

# Global Instagram client (initialized in the lifespan; transport-agnostic).
instagram_client: Optional[InstagramClient] = None

settings = get_settings()

# Connector logo, served at /logo.png and advertised in serverInfo.icons.
LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "logo.png"


def _client() -> InstagramClient:
    """Return the shared Instagram client, lazily creating it if needed."""
    global instagram_client
    if instagram_client is None:
        instagram_client = InstagramClient()
    return instagram_client


@asynccontextmanager
async def lifespan(_server: "FastMCP") -> AsyncIterator[None]:
    """Initialize the Instagram client and validate its token on startup."""
    global instagram_client
    logger.info("Starting Instagram MCP Server", version=settings.mcp_server_version)
    instagram_client = InstagramClient()

    # Validate the Instagram access token on startup. This is non-fatal: a remote
    # server should still come up (and serve health/auth) so the issue is
    # observable, rather than crash-looping on a transient Graph API hiccup.
    try:
        if await instagram_client.validate_access_token():
            logger.info("Instagram access token validated successfully")
        else:
            logger.error("Invalid Instagram access token (server starting anyway)")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Failed to validate access token (server starting anyway)",
            error=str(exc),
        )

    yield


# OAuth broker (Authorization Server) — also reached by the Entra callback route.
auth_provider: Optional[IGMcpAuthProvider] = None


def build_server() -> FastMCP:
    """Construct the FastMCP server, wiring the OAuth broker when auth is enabled."""
    global auth_provider
    kwargs: Dict[str, Any] = dict(
        name=settings.mcp_server_name,
        host=settings.mcp_host,
        port=settings.mcp_port,
        lifespan=lifespan,
    )
    # Branding shown by MCP clients (needs an absolute, client-reachable URL).
    if settings.server_public_url:
        base = settings.server_public_url.rstrip("/")
        kwargs["website_url"] = base
        kwargs["icons"] = [
            Icon(src=f"{base}/logo.png", mimeType="image/png", sizes=["512x512"])
        ]
    if settings.auth_enabled:
        auth_provider = IGMcpAuthProvider(settings)
        kwargs["auth_server_provider"] = auth_provider
        kwargs["auth"] = build_auth_settings(settings)
        logger.info("OAuth broker (Entra ID) enabled")
    else:
        logger.warning("Authentication DISABLED (local/dev mode)")
    return FastMCP(**kwargs)


mcp = build_server()


def tool_handler(name: str, **extra_meta: Any):
    """Wrap a tool body in the shared MCPToolResult envelope + error handling.

    The decorated function returns its raw ``data`` payload; this wrapper attaches
    standard metadata and returns the ``MCPToolResult`` as a dict. FastMCP then
    emits both a JSON text block (the same envelope clients saw before) and the
    corresponding structured output.
    """

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            try:
                data = await fn(*args, **kwargs)
                metadata: Dict[str, Any] = {
                    "tool": name,
                    "timestamp": datetime.utcnow().isoformat(),
                }
                metadata.update(extra_meta)
                result = MCPToolResult(success=True, data=data, metadata=metadata)
            except InstagramAPIError as e:
                logger.error("Instagram API error", tool=name, error=str(e))
                result = MCPToolResult(
                    success=False,
                    error=f"Instagram API error: {e.message}",
                    metadata={
                        "error_code": e.error_code,
                        "error_subcode": e.error_subcode,
                    },
                )
            except Exception as e:  # noqa: BLE001
                logger.error("Tool execution error", tool=name, error=str(e))
                result = MCPToolResult(
                    success=False, error=f"Tool execution failed: {str(e)}"
                )
            return result.model_dump(mode="json")

        return wrapper

    return deco


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
@tool_handler("get_profile_info")
async def get_profile_info(account_id: Optional[str] = None) -> Dict[str, Any]:
    """Get Instagram business profile information including followers, bio, and account details."""
    profile = await _client().get_profile_info(account_id)
    return profile.model_dump(mode="json")


@mcp.tool()
@tool_handler("get_media_posts")
async def get_media_posts(
    account_id: Optional[str] = None,
    limit: int = 25,
    after: Optional[str] = None,
) -> Dict[str, Any]:
    """Get recent media posts from Instagram account with engagement metrics."""
    posts = await _client().get_media_posts(account_id, limit, after)
    return {
        "posts": [post.model_dump(mode="json") for post in posts],
        "count": len(posts),
    }


@mcp.tool()
@tool_handler("get_media_insights")
async def get_media_insights(
    media_id: str, metrics: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Get detailed insights and analytics for a specific Instagram post.

    metrics may include: reach, likes, comments, shares, saved, video_views
    (video_views only works for video posts).
    """
    parsed = [InsightMetric(m) for m in metrics] if metrics else None
    insights = await _client().get_media_insights(media_id, parsed)
    return {
        "media_id": media_id,
        "insights": [insight.model_dump(mode="json") for insight in insights],
    }


@mcp.tool()
@tool_handler("publish_media")
async def publish_media(
    image_url: Optional[str] = None,
    video_url: Optional[str] = None,
    caption: Optional[str] = None,
    location_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload and publish an image or video to Instagram with caption and optional location.

    Provide either image_url or video_url (must be publicly accessible URLs).
    """
    request = PublishMediaRequest(
        image_url=image_url,
        video_url=video_url,
        caption=caption,
        location_id=location_id,
    )
    response = await _client().publish_media(request)
    return response.model_dump(mode="json")


@mcp.tool()
@tool_handler("get_account_pages")
async def get_account_pages() -> Dict[str, Any]:
    """Get Facebook pages connected to the account and their Instagram business accounts."""
    pages = await _client().get_account_pages()
    return {
        "pages": [page.model_dump(mode="json") for page in pages],
        "count": len(pages),
    }


@mcp.tool()
@tool_handler("get_account_insights")
async def get_account_insights(
    account_id: Optional[str] = None,
    metrics: Optional[List[str]] = None,
    period: str = "day",
    breakdown: Optional[str] = None,
    timeframe: Optional[str] = None,
) -> Dict[str, Any]:
    """Get account-level insights and analytics for an Instagram business account.

    Engagement metrics (use period='day'): reach, profile_views, website_clicks,
    accounts_engaged.

    Audience demographics (use period='lifetime' AND set breakdown + timeframe):
      metrics: follower_demographics, reached_audience_demographics,
               engaged_audience_demographics
      breakdown: one of age, gender, city, country
      timeframe: one of last_14_days, last_30_days, last_90_days, prev_month,
                 this_month, this_week (engaged/reached currently only return data
                 for this_week and this_month)
    Demographics require the instagram_manage_insights permission and enough audience
    volume — Meta withholds breakdowns for small/low-activity accounts and returns an
    empty result. Results come back under each metric's `total_value.breakdowns`.

    Example (audience age):
      metrics=["follower_demographics"], period="lifetime",
      breakdown="age", timeframe="last_30_days"
    """
    period_enum = InsightPeriod(period)
    insights = await _client().get_account_insights(
        account_id, metrics, period_enum, breakdown=breakdown, timeframe=timeframe
    )
    has_demographics = any(
        bd.get("results")
        for ins in insights
        for bd in (ins.total_value or {}).get("breakdowns", [])
    )
    result: Dict[str, Any] = {
        "insights": [insight.model_dump(mode="json") for insight in insights],
        "period": period_enum.value,
        "breakdown": breakdown,
        "timeframe": timeframe,
    }
    if breakdown and not has_demographics:
        result["note"] = (
            "No demographic breakdown was returned. Instagram withholds audience "
            "demographics when the audience in the requested timeframe is below its "
            "privacy threshold (small or low-activity accounts). This is expected "
            "API behavior, not an error — try timeframe 'this_month', or check back "
            "as the audience grows."
        )
    return result


@mcp.tool()
@tool_handler("get_facebook_posts")
async def get_facebook_posts(
    page_id: Optional[str] = None, limit: int = 25
) -> Dict[str, Any]:
    """Get recent Facebook Page posts with engagement counts.

    Returns each post's reactions, comments, and shares counts plus message,
    permalink, and date. page_id defaults to the first connected Facebook Page.
    Requires a Page with pages_read_engagement access.

    Note: Facebook no longer exposes per-post impressions/reach for organic posts;
    this returns reliable engagement counts instead.
    """
    posts = await _client().get_facebook_posts(page_id, limit)
    return {
        "posts": [post.model_dump(mode="json") for post in posts],
        "count": len(posts),
    }


@mcp.tool()
@tool_handler("get_facebook_page_insights")
async def get_facebook_page_insights(
    page_id: Optional[str] = None,
    metrics: Optional[List[str]] = None,
    period: str = "day",
) -> Dict[str, Any]:
    """Get Facebook Page-level insights (impressions, engagement, follows, views).

    page_id defaults to the first connected Page; period is 'day', 'week', or
    'days_28'. Requires a Page with read_insights access.

    Note: Meta has deprecated most organic Facebook insight metrics, so results are
    frequently empty — when nothing is returned, a `note` explains why.
    """
    period_enum = InsightPeriod(period)
    insights = await _client().get_facebook_page_insights(page_id, metrics, period_enum)
    has_data = any(
        (ins.values or []) or (ins.total_value or {}).get("value") is not None
        for ins in insights
    )
    result: Dict[str, Any] = {
        "insights": [insight.model_dump(mode="json") for insight in insights],
        "period": period_enum.value,
    }
    if not has_data:
        result["note"] = (
            "No Facebook Page insight data was returned. Meta has deprecated most "
            "organic Page/post insight metrics, and remaining ones can be empty for "
            "low-activity accounts. This is expected API behavior, not an error — "
            "per-post engagement is available via get_facebook_posts."
        )
    return result


@mcp.tool()
@tool_handler("validate_access_token")
async def validate_access_token() -> Dict[str, Any]:
    """Validate the Instagram API access token and check permissions."""
    is_valid = await _client().validate_access_token()
    return {"valid": is_valid}


@mcp.tool()
@tool_handler("get_conversations", note="Requires instagram_manage_messages permission")
async def get_conversations(
    page_id: Optional[str] = None, limit: int = 25
) -> Dict[str, Any]:
    """Get Instagram DM conversations. Requires instagram_manage_messages permission.

    Lists all conversations for the connected Instagram account. page_id is
    auto-detected from connected pages if not provided.
    """
    conversations = await _client().get_conversations(page_id, limit)
    return {
        "conversations": [conv.model_dump(mode="json") for conv in conversations],
        "count": len(conversations),
    }


@mcp.tool()
@tool_handler("get_conversation_messages")
async def get_conversation_messages(
    conversation_id: str, limit: int = 25
) -> Dict[str, Any]:
    """Get messages from a specific Instagram DM conversation.

    Requires instagram_manage_messages permission. Use get_conversations for IDs.
    """
    messages = await _client().get_conversation_messages(conversation_id, limit)
    return {
        "conversation_id": conversation_id,
        "messages": [msg.model_dump(mode="json") for msg in messages],
        "count": len(messages),
    }


@mcp.tool()
@tool_handler(
    "send_dm",
    note="24-hour response window applies. Requires Advanced Access.",
)
async def send_dm(recipient_id: str, message: str) -> Dict[str, Any]:
    """Send an Instagram direct message to a user.

    IMPORTANT: Requires instagram_manage_messages with Advanced Access from Meta.
    Can only reply within 24 hours of the user's last message; the recipient must
    have initiated the conversation. recipient_id is the Instagram Scoped User ID
    (IGSID); message is at most 1000 characters.
    """
    request = SendDMRequest(recipient_id=recipient_id, message=message)
    response = await _client().send_dm(request)
    return response.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource(
    "instagram://profile",
    name="Instagram Profile",
    description="Current Instagram business profile information",
    mime_type="application/json",
)
async def resource_profile() -> str:
    try:
        profile = await _client().get_profile_info()
        return json.dumps(profile.model_dump(mode="json"), indent=2)
    except Exception as e:  # noqa: BLE001
        logger.error("Resource read error", uri="instagram://profile", error=str(e))
        return json.dumps({"error": str(e)}, indent=2)


@mcp.resource(
    "instagram://media/recent",
    name="Recent Media Posts",
    description="Recent Instagram posts with engagement metrics",
    mime_type="application/json",
)
async def resource_recent_media() -> str:
    try:
        posts = await _client().get_media_posts(limit=10)
        return json.dumps([post.model_dump(mode="json") for post in posts], indent=2)
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Resource read error", uri="instagram://media/recent", error=str(e)
        )
        return json.dumps({"error": str(e)}, indent=2)


@mcp.resource(
    "instagram://insights/account",
    name="Account Insights",
    description="Account-level analytics and insights",
    mime_type="application/json",
)
async def resource_account_insights() -> str:
    try:
        insights = await _client().get_account_insights()
        return json.dumps(
            [insight.model_dump(mode="json") for insight in insights], indent=2
        )
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Resource read error", uri="instagram://insights/account", error=str(e)
        )
        return json.dumps({"error": str(e)}, indent=2)


@mcp.resource(
    "instagram://pages",
    name="Connected Pages",
    description="Facebook pages connected to the account",
    mime_type="application/json",
)
async def resource_pages() -> str:
    try:
        pages = await _client().get_account_pages()
        return json.dumps([page.model_dump(mode="json") for page in pages], indent=2)
    except Exception as e:  # noqa: BLE001
        logger.error("Resource read error", uri="instagram://pages", error=str(e))
        return json.dumps({"error": str(e)}, indent=2)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


@mcp.prompt(
    name="analyze_engagement",
    description="Analyze Instagram post engagement and provide insights",
)
async def analyze_engagement(
    media_id: str, comparison_period: Optional[str] = None
) -> str:
    """Analyze the engagement metrics for an Instagram post."""
    try:
        insights = await _client().get_media_insights(media_id)
        return f"""
Analyze the engagement metrics for Instagram post {media_id}:

Insights Data:
{json.dumps([insight.model_dump(mode='json') for insight in insights], indent=2)}

Please provide:
1. Overall engagement performance assessment
2. Key metrics analysis (impressions, reach, likes, comments, shares)
3. Engagement rate calculation and interpretation
4. Recommendations for improving future posts
5. Comparison with typical performance benchmarks
"""
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Prompt generation error", prompt="analyze_engagement", error=str(e)
        )
        return f"Error generating prompt: {str(e)}"


@mcp.prompt(
    name="content_strategy",
    description="Generate content strategy recommendations based on account performance",
)
async def content_strategy(
    focus_area: str = "engagement", time_period: str = "week"
) -> str:
    """Generate a content strategy for Instagram."""
    try:
        posts = await _client().get_media_posts(limit=20)
        account_insights = await _client().get_account_insights()
        return f"""
Generate a content strategy for Instagram focusing on {focus_area} over the {time_period}:

Recent Posts Performance:
{json.dumps([post.model_dump(mode='json') for post in posts[:5]], indent=2)}

Account Insights:
{json.dumps([insight.model_dump(mode='json') for insight in account_insights], indent=2)}

Please provide:
1. Content performance analysis
2. Optimal posting times and frequency
3. Content type recommendations (images, videos, carousels)
4. Caption and hashtag strategies
5. Engagement tactics to improve {focus_area}
6. Specific action items for the next {time_period}
"""
    except Exception as e:  # noqa: BLE001
        logger.error("Prompt generation error", prompt="content_strategy", error=str(e))
        return f"Error generating prompt: {str(e)}"


@mcp.prompt(
    name="hashtag_analysis",
    description="Analyze hashtag performance and suggest improvements",
)
async def hashtag_analysis(post_count: int = 10) -> str:
    """Analyze hashtag performance for recent posts."""
    try:
        posts = await _client().get_media_posts(limit=post_count)
        hashtags_data = []
        for post in posts:
            if post.caption:
                hashtags = [
                    word for word in post.caption.split() if word.startswith("#")
                ]
                hashtags_data.append(
                    {
                        "post_id": post.id,
                        "hashtags": hashtags,
                        "likes": post.like_count,
                        "comments": post.comments_count,
                    }
                )
        return f"""
Analyze hashtag performance for the last {post_count} Instagram posts:

Hashtag Data:
{json.dumps(hashtags_data, indent=2)}

Please provide:
1. Most frequently used hashtags
2. Hashtag performance correlation with engagement
3. Hashtag diversity analysis
4. Recommendations for hashtag optimization
5. Suggested new hashtags to try
6. Hashtag strategy improvements
"""
    except Exception as e:  # noqa: BLE001
        logger.error("Prompt generation error", prompt="hashtag_analysis", error=str(e))
        return f"Error generating prompt: {str(e)}"


# ---------------------------------------------------------------------------
# Health check (unauthenticated, for container/load-balancer probes)
# ---------------------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Lightweight liveness probe — does not touch Instagram or require auth."""
    return JSONResponse({"status": "ok", "service": settings.mcp_server_name})


@mcp.custom_route("/logo.png", methods=["GET"])
async def logo(_request: Request) -> FileResponse:
    """Serve the connector logo (unauthenticated) referenced by serverInfo.icons."""
    return FileResponse(LOGO_PATH, media_type="image/png")


@mcp.custom_route("/favicon.ico", methods=["GET"])
async def favicon(_request: Request) -> FileResponse:
    """Serve the logo as the site favicon too — some clients derive the connector
    icon from the domain favicon rather than serverInfo.icons. Without this, the
    fallback resolves up to the parent domain's (wrong) favicon."""
    return FileResponse(LOGO_PATH, media_type="image/png")


@mcp.custom_route("/", methods=["GET"])
async def index(_request: Request) -> HTMLResponse:
    """Minimal landing page declaring the icon, so favicon resolvers that parse the
    page HTML (rather than serverInfo.icons) find our logo instead of falling back
    to the parent domain's favicon."""
    base = (settings.server_public_url or "").rstrip("/")
    return HTMLResponse(
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>{settings.mcp_server_name}</title>"
        '<link rel="icon" type="image/png" href="/logo.png">'
        '<link rel="apple-touch-icon" href="/logo.png">'
        f'<meta property="og:image" content="{base}/logo.png">'
        "</head><body>Instagram MCP server</body></html>"
    )


@mcp.custom_route("/oauth/entra/callback", methods=["GET"])
async def entra_callback(request: Request):
    """Entra redirects here after M365 login; the broker completes the exchange and
    redirects back to the MCP client (Claude). Unauthenticated by design."""
    if auth_provider is None:
        return JSONResponse({"error": "auth disabled"}, status_code=404)
    target = await auth_provider.handle_entra_callback(
        code=request.query_params.get("code"),
        state=request.query_params.get("state"),
        error=request.query_params.get("error"),
    )
    if target is None:
        return JSONResponse(
            {"error": "invalid or expired login state"}, status_code=400
        )
    return RedirectResponse(target, status_code=302)


def _configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    import logging

    logging.basicConfig(level=getattr(logging, settings.log_level))


async def main() -> None:
    """Main entry point. Serves Streamable HTTP by default, or stdio for local dev."""
    _configure_logging()

    if settings.mcp_transport == "stdio":
        await mcp.run_stdio_async()
    else:
        await mcp.run_streamable_http_async()


if __name__ == "__main__":
    asyncio.run(main())
