"""Constants for the Renovate Updates integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "renovate_updates"

CONF_REPOSITORY: Final = "repository"
CONF_PR_AUTHOR: Final = "pr_author"
CONF_MERGE_METHOD: Final = "merge_method"
CONF_UPDATE_METHOD: Final = "update_method"
CONF_SCAN_INTERVAL_MINUTES: Final = "scan_interval_minutes"
CONF_FALLBACK_INTERVAL_MINUTES: Final = "fallback_interval_minutes"

UPDATE_METHOD_POLL: Final = "poll"
UPDATE_METHOD_WEBHOOK: Final = "webhook"
UPDATE_METHODS: Final = [UPDATE_METHOD_POLL, UPDATE_METHOD_WEBHOOK]

MERGE_METHODS: Final = ["squash", "merge", "rebase"]

DEFAULT_PR_AUTHOR: Final = "renovate[bot]"
DEFAULT_MERGE_METHOD: Final = "squash"
DEFAULT_UPDATE_METHOD: Final = UPDATE_METHOD_POLL
DEFAULT_SCAN_INTERVAL_MINUTES: Final = 30

# In webhook mode the integration still performs a slow background poll so a
# dropped delivery cannot leave the entity list permanently stale. Set to 0 in
# the options to turn it off and rely on webhooks alone.
DEFAULT_FALLBACK_INTERVAL_MINUTES: Final = 60

GITHUB_API: Final = "https://api.github.com"
API_VERSION: Final = "2022-11-28"

# Maximum open pull requests to consider in one query.
PR_QUERY_LIMIT: Final = 50

# How far back through merged pull requests to look when discovering which
# dependencies exist. Only titles are fetched for these, so the cost is small.
PR_HISTORY_LIMIT: Final = 100
