# Linkly Connector Example

## Connector overview
This connector syncs data from [Linkly](https://linklyhq.com), a URL shortener and click tracker, into your destination using the Fivetran Connector SDK. It reads the public [Linkly REST API](https://linklyhq.com/support/api) (endpoint reference: https://api.linklyhq.com/api/openapi) and delivers five tables: workspaces, links (with lifetime and 30-day click counters, UTM fields, tracking tag IDs and routing rules), custom domains, clicks per workspace per day, and conversion events.

Typical use cases are joining short-link click data to ad spend and CRM data in the warehouse, campaign attribution by UTM parameter, and reporting on the conversions that Linkly attributed to a link.

## Accreditation
This example was contributed by [Linkly](https://linklyhq.com), the vendor of the source application.

## Requirements
- [Supported Python versions](https://github.com/fivetran/community_connectors/blob/main/README.md#requirements)
- Operating system:
  - Windows: 10 or later (64-bit only)
  - macOS: 13 (Ventura) or later (Apple Silicon [arm64] or Intel [x86_64])
  - Linux: Distributions such as Ubuntu 20.04 or later, Debian 10 or later, or Amazon Linux 2 or later (arm64 or x86_64)

## Getting started
Refer to the [Connector SDK Setup Guide](https://fivetran.com/docs/connectors/connector-sdk/setup-guide) to get started.

To initialize a new Connector SDK project using this connector as a starting point, run:

```bash
fivetran init --template linkly
```

`fivetran init` initializes a new Connector SDK project by setting up the project structure, configuration files, and a connector you can run immediately with `fivetran debug`. For more information on `fivetran init`, refer to the [Connector SDK `init` documentation](https://fivetran.com/docs/connector-sdk/connector-development-and-configuration/connector-sdk-commands#fivetraninit).

> Note: Ensure you have updated the `configuration.json` file with the necessary parameters before running `fivetran debug`. See the [Configuration file](#configuration-file) section for details on the required configuration parameters.

## Features
- Syncs every workspace the API key can access, or a configured subset of workspace IDs.
- Full re-import of `link` and `domain` on every sync so counters and edits are always current (the Linkly API has no modification timestamp on links). Domains removed in Linkly are deleted from the destination.
- Incremental daily click totals per workspace with a two-day replay window, so days that were still in progress are corrected on the next sync.
- Incremental conversions using the ULID `id` as the cursor.
- Retry with exponential backoff on HTTP 429 (rate limit), 5xx, timeouts and connection errors; immediate failure with a clear message on 401 and other 4xx responses.
- Checkpoints after every page of links, every click window and every table, so an interrupted sync resumes rather than restarts.
- Runs end to end without credentials against the bundled mock API for local testing.

## Configuration file
The connector reads the following keys from `configuration.json`, which is uploaded to Fivetran at deploy time. Remove the optional keys you do not need, or replace their placeholders with real values.

```json
{
  "api_key": "<YOUR_LINKLY_API_KEY>",
  "start_date": "<OPTIONAL_FIRST_DAY_OF_CLICK_HISTORY_AS_YYYY-MM-DD>",
  "workspace_ids": "<OPTIONAL_COMMA_SEPARATED_LINKLY_WORKSPACE_IDS>",
  "base_url": "<OPTIONAL_LINKLY_API_BASE_URL_FOR_TESTING>"
}
```

- `api_key` (required) – A Linkly API key. One key can read every workspace its user is a member of. See [Authentication](#authentication).
- `start_date` (optional) – `YYYY-MM-DD`. The first day of click history to sync into `click_daily`. Defaults to `2019-01-01`, the earliest date Linkly holds per-day click counts for. Must not be in the future.
- `workspace_ids` (optional) – Comma-separated workspace IDs to restrict the sync, for example `42,43`. Defaults to all workspaces the key can access.
- `base_url` (optional, testing only) – Overrides the API base URL `https://api.linklyhq.com/api/v1`. Used by `tests/mock-configuration.json` to point the connector at the local mock API.

All values are validated in `validate_configuration()` before any request is made.

> Note: When submitting connector code as a community connector in the open-source [Community Connector repository](https://github.com/fivetran/community_connectors/tree/main), ensure the `configuration.json` file has placeholder values. When adding the connector to your production repository, ensure that the `configuration.json` file is not checked into version control to protect sensitive information.

## Requirements file
The connector uses only the Python standard library and the `requests` package, which is pre-installed in the Fivetran environment, so no `requirements.txt` file is needed.

> Note: [Some packages](https://fivetran.com/docs/connector-sdk/technical-reference#preinstalledpackages) are pre-installed in the Connector SDK runtime environment. To avoid dependency conflicts, do not declare them in your `requirements.txt`.

## Authentication
Linkly uses API keys. The connector sends the key as a bearer token on every request (`Authorization: Bearer <api_key>`), which is the method Linkly recommends for production; it never places the key in a query string. Refer to `build_session()` in `connector.py`.

To obtain a key:

1. Log in to [Linkly](https://app.linklyhq.com).
2. Open **Settings**, then the **API** section of your workspace.
3. Copy the API key into the `api_key` value of `configuration.json`.

The [Linkly API documentation](https://linklyhq.com/support/api) has screenshots of these steps.

## Pagination
- `link` – `GET /api/v1/workspace/{id}/list_links` is page-number based (`page`, `page_size`; the response carries `page_number` and `total_pages`). The connector requests 100 links per page sorted by `id` ascending so the sequence is stable while new links are being created, exits on the last page or an empty page, and checkpoints the next page number in state under `link_resume_page` so an interrupted sync resumes at the same page. Active links and trashed links (`deleted=true`) are listed as two separate passes. Refer to `sync_links()` and `fetch_links_page()`.
- `conversion` – `GET /api/v1/conversions` has no pagination; it returns at most the 1,000 most recent rows. Refer to `sync_conversions()`.
- `workspace`, `domain`, `click_daily` – Single-response endpoints. Click history is requested in windows of up to 366 days per API call, with one call for all clicks and one with `bots=false` for human clicks. Refer to `sync_click_daily()` and `upsert_click_window()`.

## Data handling
- Only primary keys and the columns whose type must not be inferred are declared in `schema()`. All other columns are inferred from the API payload, so new fields that Linkly adds to a link or conversion flow through without a connector change.
- `link.rules`, `link.sparkline` and `conversion.metadata` are declared as `JSON` columns and passed to the SDK as Python objects; the SDK serialises them (pre-encoding with `json.dumps` would double-encode). `conversion.occurred_at` and `conversion.inserted_at` are `UTC_DATETIME`, and `click_daily.date` is a `NAIVE_DATE`. Refer to `schema()`.
- Trashed links are fetched with `deleted=true` and upserted with `deleted = true` rather than removed, so their historical counters stay queryable. Links purged from the trash are not detected. Refer to `build_link_row()`.
- `click_daily.clicks` includes bot traffic; `click_daily.human_clicks` is the same range requested with `bots=false`. Dates are UTC calendar days, so totals can differ from the Linkly dashboard when the workspace timezone is not UTC.
- State layout: `{"link_resume_page": {"<workspace_id>:<active|deleted>": <page>}, "click_cursor_by_workspace": {"<workspace_id>": "YYYY-MM-DD"}, "conversion_cursor": "<ulid>", "domain_names_by_workspace": {"<workspace_id>": ["<domain name>"]}}`. `link_resume_page` entries exist only while a workspace's links are partially synced. `domain_names_by_workspace` holds the domain names delivered on the previous sync; names that no longer appear are removed with `op.delete()`. Refer to `sync_domains()`.
- The `conversion` cursor comparison is strict (`id > cursor`) because ULIDs are unique and sort chronologically. Refer to `sync_conversions()`.
- Column names follow Fivetran's naming rules in the destination, so for example `link.ga4_tag_id` arrives as `ga_4_tag_id`.

## Error handling
Refer to `get_json()`, `raise_permanent_error()` and `wait_before_retry()` in `connector.py`.

- 401 – Raises `RuntimeError` with a message pointing at the Linkly API key settings; not retried.
- Other 4xx – Raises `RuntimeError` with the `error` or `message` field from the response body; not retried. Linkly returns 404 for workspaces the key cannot access, which cannot happen for workspaces returned by `GET /api/v1/workspaces`.
- 429 – The response body's `current_usage`/`limit` is logged and the request is retried with exponential backoff (2, 4, 8, ... up to 120 seconds, 6 attempts). Linkly does not send a `Retry-After` header; if one is present, in either the seconds or the HTTP-date form, it is honoured instead, capped at 120 seconds.
- 5xx, timeouts and connection errors – Retried with the same backoff; after 6 failed attempts a `RuntimeError` fails the sync.
- Conversion overflow – If the endpoint returns its full 1,000 rows and all of them are newer than the cursor, or the initial sync receives 1,000 rows, a warning is logged because older conversions may not have been returned.

## Tables created
The connector creates five tables (refer to `schema()`):

| Table         | Primary key             | Sync        | Columns                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ------------- | ----------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `workspace`   | `id`                    | full        | `id` (LONG), `name`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `link`        | `id`                    | full        | `id` (LONG), `workspace_id` (LONG), `name`, `url`, `full_url`, `domain`, `slug`, `note`, `enabled`, `deleted`, `cloaking`, `hide_referrer`, `forward_params`, `block_bots`, `public_analytics`, `utm_source`, `utm_medium`, `utm_campaign`, `utm_term`, `utm_content`, `og_title`, `og_description`, `og_image`, `ga4_tag_id`, `gtm_id`, `fb_pixel_id`, `tiktok_pixel_id`, `linkify_words`, `replacements`, `head_tags`, `body_tags`, `rules` (JSON), `sparkline` (JSON), `clicks_total`, `clicks_today`, `clicks_thirty_days`, `human_clicks_total`, `human_clicks_today`, `human_clicks_previous_day`, `human_clicks_thirty_days` |
| `domain`      | `workspace_id`, `name`  | full        | `workspace_id` (LONG), `name`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `click_daily` | `workspace_id`, `date`  | incremental | `workspace_id` (LONG), `date` (NAIVE_DATE), `clicks`, `human_clicks`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `conversion`  | `id`                    | incremental | `id`, `link_id` (LONG), `click_id`, `event_name`, `event_type`, `event_id`, `external_id`, `amount_cents` (LONG), `currency`, `country`, `ip_source`, `source`, `metadata` (JSON), `occurred_at` (UTC_DATETIME), `inserted_at` (UTC_DATETIME)                                                                                                                                                                                                                                                                                                       |

Relationships: `link.workspace_id`, `domain.workspace_id` and `click_daily.workspace_id` reference `workspace.id`. `conversion.link_id` references `link.id` and is null when the conversion could not be attributed. `conversion.amount_cents` is an integer in minor units of `conversion.currency`.

## Additional files
The connector uses the following additional files:
- **`tests/mock_linkly_api.py`** – A standard-library HTTP server that imitates the five Linkly endpoints with the response shapes from the OpenAPI spec, including one simulated HTTP 429 and bearer-token checking, so `fivetran debug` can run without a real API key. Start it with `python tests/mock_linkly_api.py 5055`. Restart it with `--drop-domain` and sync again to exercise domain deletion, or pass `--conversion-count <n>` to change how many conversions exist.
- **`tests/mock-configuration.json`** – Configuration that points the connector at the mock server. Run `fivetran debug --configuration tests/mock-configuration.json` from the connector directory while the mock is running.

## Additional considerations
The examples provided are intended to help you effectively use Fivetran's Connector SDK. While we've tested the code, Fivetran cannot be held responsible for any unexpected or negative consequences that may arise from using these examples. For inquiries, please reach out to our Support team.
