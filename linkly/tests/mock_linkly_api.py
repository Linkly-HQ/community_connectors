"""Stand-in for api.linklyhq.com so the Linkly connector can be exercised without an API key.

Serves the five read endpoints the connector uses with the response shapes from the public
OpenAPI spec (https://api.linklyhq.com/api/openapi). It also returns one HTTP 429 on the first
list_links call so the retry and backoff path is exercised, and rejects requests that do not
carry the mock bearer token so the authentication path is exercised too.

Run from the connector directory:
    python tests/mock_linkly_api.py 5055
Then, in another terminal:
    fivetran debug --configuration tests/mock-configuration.json

To exercise domain deletion, restart the mock with --drop-domain (workspace 43 then no longer
returns ws43.example.com) and run fivetran debug again without resetting the state. Use
--conversion-count to change how many conversions exist (the endpoint returns at most 1,000).
"""

# For parsing the port and the scenario flags
import argparse

# For encoding JSON response bodies
import json

# For the port argument and quiet request logging
import sys

# For generating the daily click series
from datetime import date, timedelta

# Standard-library HTTP server; no third-party dependencies are needed for the mock
from http.server import BaseHTTPRequestHandler, HTTPServer

# For parsing the request path and query string
from urllib.parse import parse_qs, urlparse

MOCK_API_KEY = "mock-api-key"
DEFAULT_PORT = 5055
WORKSPACES = [{"id": 42, "name": "Acme Marketing"}, {"id": 43, "name": "Acme Sales"}]
LINKS_PER_WORKSPACE = 230  # Forces three pages at page_size=100
TRASHED_LINKS_PER_WORKSPACE = 3
TRASHED_LINK_ID_OFFSET = 900
DEFAULT_CONVERSION_COUNT = 1200  # More than the endpoint's 1,000-row maximum
DROPPABLE_DOMAIN = {"workspace_id": 43, "name": "ws43.example.com"}  # Removed by --drop-domain
RATE_LIMIT_ONCE = {"is_armed": True}
SCENARIO = {"drop_domain": False}


def make_link(workspace_id, number, is_deleted=False):
    """
    Build one link object in the shape of the list_links response.
    Args:
        workspace_id: The workspace the link belongs to.
        number: A sequence number used to derive the id and vary the field values.
        is_deleted: Whether the link is in the trash.
    Returns:
        The link dictionary.
    """
    return {
        "id": workspace_id * 1000 + number,
        "workspace_id": workspace_id,
        "name": f"Link {number}",
        "url": f"https://www.example.com/landing/{number}?ref=linkly",
        "full_url": f"https://go.example.com/l{number}",
        "domain": "go.example.com",
        "slug": f"/l{number}",
        "enabled": True,
        "deleted": is_deleted,
        "cloaking": False,
        "hide_referrer": None,
        "forward_params": True,
        "block_bots": False,
        "public_analytics": False,
        "utm_source": "newsletter" if number % 2 else None,
        "utm_campaign": "summer_sale",
        "og_title": None,
        "ga4_tag_id": "G-XXXX" if number % 5 == 0 else None,
        "rules": (
            [{"what": "country", "matches": "US", "url": "https://us.example.com"}]
            if number % 7 == 0
            else []
        ),
        "clicks_total": number * 13,
        "clicks_today": number % 4,
        "clicks_thirty_days": number * 2,
        "human_clicks_total": number * 11,
        "human_clicks_today": number % 3,
        "human_clicks_previous_day": number % 5,
        "human_clicks_thirty_days": number,
        "sparkline": [number % 3, number % 5, number % 7],
    }


def make_conversions(conversion_count):
    """
    Build the conversion list in the shape of the conversions response, most recent first.
    Args:
        conversion_count: How many conversions to generate.
    Returns:
        A list of conversion dictionaries with ULID-like ids that sort in creation order.
    """
    conversions = []
    for index in range(conversion_count):
        conversions.append(
            {
                "id": f"01K{index:023d}",
                "link_id": 42000 + (index % 50),
                "click_id": f"01C{index:023d}",
                "event_name": "purchase",
                "event_type": "sale",
                "event_id": f"shop-{index}",
                "external_id": None,
                "amount_cents": 4999,
                "currency": "USD",
                "country": "US",
                "ip_source": "browser",
                "source": "shopify",
                "metadata": {"sku": f"SKU-{index}"},
                "occurred_at": "2026-09-01T10:00:00Z",
                "inserted_at": "2026-09-01T10:00:05Z",
            }
        )
    conversions.reverse()
    return conversions


CONVERSIONS = make_conversions(DEFAULT_CONVERSION_COUNT)


class MockLinklyHandler(BaseHTTPRequestHandler):
    """Routes GET requests to the mock implementations of the Linkly endpoints."""

    def log_message(self, format, *args):
        """Write request lines to stderr with a prefix instead of the default noisy format."""
        sys.stderr.write("mock: " + (format % args) + "\n")

    def send_json(self, status, payload):
        """Send a JSON response with the given HTTP status."""
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """Dispatch the request by path, mirroring the real API's routes."""
        if self.headers.get("Authorization") != f"Bearer {MOCK_API_KEY}":
            return self.send_json(401, {"error": "Not authorized", "code": "unauthorized"})

        url = urlparse(self.path)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        parts = url.path.strip("/").split("/")  # ["api", "v1", ...]

        if parts[2:] == ["workspaces"]:
            return self.send_json(200, WORKSPACES)
        if parts[2:] == ["conversions"]:
            limit = int(query.get("limit", 100))
            return self.send_json(200, {"conversions": CONVERSIONS[:limit]})
        if len(parts) == 5 and parts[2] == "workspace":
            return self.handle_workspace_resource(int(parts[3]), parts[4], query)
        return self.send_json(404, {"error": "Not found", "code": "not_found"})

    def handle_workspace_resource(self, workspace_id, resource, query):
        """Serve the list_links, domains and clicks endpoints for one workspace."""
        if workspace_id not in {workspace["id"] for workspace in WORKSPACES}:
            return self.send_json(404, {"error": "Not found", "code": "not_found"})
        if resource == "list_links":
            return self.send_links(workspace_id, query)
        if resource == "domains":
            domains = [{"name": "go.example.com"}, {"name": f"ws{workspace_id}.example.com"}]
            if SCENARIO["drop_domain"] and workspace_id == DROPPABLE_DOMAIN["workspace_id"]:
                domains = [
                    domain for domain in domains if domain != {"name": DROPPABLE_DOMAIN["name"]}
                ]
            return self.send_json(200, {"domains": domains})
        if resource == "clicks":
            return self.send_clicks(workspace_id, query)
        return self.send_json(404, {"error": "Not found", "code": "not_found"})

    def send_links(self, workspace_id, query):
        """Serve one page of active or trashed links, with a single simulated rate limit."""
        if RATE_LIMIT_ONCE["is_armed"]:
            RATE_LIMIT_ONCE["is_armed"] = False
            return self.send_json(
                429,
                {
                    "error": "rate_limit_exceeded",
                    "message": "Slow down",
                    "current_usage": 101,
                    "limit": 100,
                },
            )
        is_deleted = query.get("deleted", "false") == "true"
        total = TRASHED_LINKS_PER_WORKSPACE if is_deleted else LINKS_PER_WORKSPACE
        page = int(query.get("page", 1))
        page_size = int(query.get("page_size", 1000))
        total_pages = max(1, -(-total // page_size))
        first = (page - 1) * page_size + 1
        links = [
            make_link(workspace_id, number, is_deleted)
            for number in range(first, min(first + page_size, total + 1))
        ]
        if is_deleted:
            links = [dict(link, id=link["id"] + TRASHED_LINK_ID_OFFSET) for link in links]
        return self.send_json(
            200,
            {
                "links": links,
                "page_number": page,
                "page_size": page_size,
                "total_entries": total,
                "total_pages": total_pages,
                "workspace_link_count": total,
            },
        )

    def send_clicks(self, workspace_id, query):
        """Serve a deterministic daily click series for the requested date range."""
        start = date.fromisoformat(query["start"][:10])
        end = date.fromisoformat(query["end"][:10])
        is_human_only = query.get("bots") == "false"
        traffic = []
        day = start
        while day <= end:
            clicks = (day.toordinal() * workspace_id) % 50
            traffic.append({"t": day.isoformat(), "y": clicks // 2 if is_human_only else clicks})
            day += timedelta(days=1)
        return self.send_json(200, {"traffic": traffic})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock Linkly API for local connector testing")
    parser.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--drop-domain",
        action="store_true",
        help=f"Stop returning {DROPPABLE_DOMAIN['name']} to exercise domain deletion",
    )
    parser.add_argument(
        "--conversion-count",
        type=int,
        default=DEFAULT_CONVERSION_COUNT,
        help="Number of conversions that exist; 1000 or more fills the endpoint's cap",
    )
    arguments = parser.parse_args()
    SCENARIO["drop_domain"] = arguments.drop_domain
    CONVERSIONS[:] = make_conversions(arguments.conversion_count)
    print(f"mock Linkly API on http://127.0.0.1:{arguments.port}/api/v1", flush=True)
    HTTPServer(("127.0.0.1", arguments.port), MockLinklyHandler).serve_forever()
