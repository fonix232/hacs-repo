"""Data coordinator for the Renovate Updates integration."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from aiohttp import ClientError, ClientSession
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_VERSION,
    DOMAIN,
    GITHUB_API,
    PR_QUERY_LIMIT,
)

_LOGGER = logging.getLogger(__name__)

# "chore(deps): update ghcr.io/homarr-labs/homarr docker tag to v1.71.0"
_DEP_RE = re.compile(
    r"update\s+(?:dependency\s+)?(\S+)\s+docker\s+(?:tag|digest)", re.I
)

# Renovate's change table: | ... | `v3.3.0` → `v3.4.0` |
#
# Current Renovate renders a real arrow (U+2192); older versions emitted ASCII
# "->", and the HTML entities turn up in bodies that have been round-tripped
# through a renderer. Accept all of them -- getting this wrong leaves
# installed_version unset, and Home Assistant shows an update entity with no
# state at all when either version is missing.
#
# Anchored on backticks, with newlines excluded from the captures so prose in
# the changelog below the table cannot match by accident.
_ARROWS = r"(?:->|→|&rarr;|&#8594;)"
_CHANGE_RE = re.compile(rf"`([^`\n]+)`\s*{_ARROWS}\s*`([^`\n]+)`")

# Fallback when the body has no change table: the version from the title. This
# is lossy on major bumps (Renovate writes "to v2" rather than "to v2.0.1").
_TITLE_VERSION_RE = re.compile(r"docker\s+(?:tag|digest)\s+to\s+(\S+)", re.I)

# Read the repository's pull requests directly rather than going through the
# `search` connection. search is backed by GitHub's search index: it is
# eventually consistent, it does not reliably return private-repository results
# depending on the token type, and it reports "found nothing" as an empty result
# rather than an error -- indistinguishable from "no open pull requests".
# repository.pullRequests is a direct object read, always current, and it fails
# loudly with a null repository when the token cannot see it.
_QUERY = """
query($owner: String!, $name: String!, $limit: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(
      states: OPEN
      first: $limit
      orderBy: {field: CREATED_AT, direction: DESC}
    ) {
      nodes {
        number
        title
        url
        isDraft
        mergeable
        body
        author { login }
      }
    }
  }
}
"""


def _normalise_author(value: str) -> str:
    """Reduce the spellings of a bot account to one comparable form.

    GitHub search syntax writes the Renovate app as `app/renovate`, while the
    account that actually authors the pull requests logs in as `renovate[bot]`.
    Accept either, so an entry configured before this changed keeps working.
    """
    return value.strip().lower().removeprefix("app/").removesuffix("[bot]")


@dataclass(slots=True)
class RenovatePullRequest:
    """A single open Renovate pull request."""

    number: int
    title: str
    url: str
    mergeable: str
    body: str
    key: str
    name: str
    installed_version: str | None
    latest_version: str | None

    @property
    def has_conflict(self) -> bool:
        """Return True when GitHub reports the PR as not mergeable."""
        return self.mergeable == "CONFLICTING"


def _parse(
    number: int, title: str, url: str, mergeable: str, body: str
) -> RenovatePullRequest:
    """Turn a raw GraphQL node into a parsed pull request."""
    body = body or ""

    # Prefer the exact `old` -> `new` pair from Renovate's change table; the
    # title truncates the target version on major bumps.
    if change := _CHANGE_RE.search(body):
        installed, latest = change.group(1), change.group(2)
    else:
        installed = None
        latest = m.group(1) if (m := _TITLE_VERSION_RE.search(title)) else None

    if dep := _DEP_RE.search(title):
        # One entity per dependency, so successive PRs for the same image reuse
        # the same entity rather than churning the registry on every bump.
        name = dep.group(1)
        key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    else:
        # Grouped or non-docker PRs have no single dependency to key on, so fall
        # back to the PR itself.
        name = title.split(":", 1)[-1].strip() or title
        key = f"pr_{number}"

    return RenovatePullRequest(
        number=number,
        title=title,
        url=url,
        mergeable=mergeable or "UNKNOWN",
        body=body,
        key=key,
        name=name,
        installed_version=installed,
        latest_version=latest,
    )


class RenovateCoordinator(DataUpdateCoordinator[dict[str, RenovatePullRequest]]):
    """Fetch open Renovate pull requests for a repository."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: ClientSession,
        repository: str,
        token: str,
        pr_author: str,
        merge_method: str,
        update_interval: timedelta | None,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({repository})",
            update_interval=update_interval,
            config_entry=entry,
        )
        self._session = session
        self._repository = repository
        self._token = token
        self._pr_author = pr_author
        self._merge_method = merge_method

    @property
    def repository(self) -> str:
        """Return the watched repository, as owner/name."""
        return self._repository

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "home-assistant-renovate-updates",
        }

    async def _async_update_data(self) -> dict[str, RenovatePullRequest]:
        """Fetch the current set of open Renovate pull requests."""
        owner, _, name = self._repository.partition("/")
        try:
            response = await self._session.post(
                f"{GITHUB_API}/graphql",
                headers=self._headers,
                json={
                    "query": _QUERY,
                    "variables": {
                        "owner": owner,
                        "name": name,
                        "limit": PR_QUERY_LIMIT,
                    },
                },
            )
            if response.status in (401, 403):
                raise ConfigEntryAuthFailed(
                    f"GitHub rejected the token (HTTP {response.status})"
                )
            if response.status != 200:
                raise UpdateFailed(
                    f"GitHub returned HTTP {response.status}: {await response.text()}"
                )
            payload = await response.json()
        except ClientError as err:
            raise UpdateFailed(f"Error talking to GitHub: {err}") from err

        # GraphQL reports errors in the body with a 200 status.
        if errors := payload.get("errors"):
            if any(
                error.get("type") in ("FORBIDDEN", "UNAUTHORIZED")
                or "not accessible by personal access token"
                in (error.get("message") or "")
                for error in errors
            ):
                # Almost always a fine-grained token without the Pull requests
                # permission: Metadata and Contents alone resolve the repository
                # but are refused on pullRequests. Raise for reauth rather than
                # retrying forever, since no amount of waiting will fix it.
                raise ConfigEntryAuthFailed(
                    f"The token cannot read pull requests on {self._repository}. "
                    "Grant it 'Pull requests: Read and write'."
                )
            raise UpdateFailed(f"GitHub GraphQL error: {errors}")

        repository = (payload.get("data") or {}).get("repository")
        if repository is None:
            # A readable repository never comes back null, so this means the
            # token cannot see it -- which is an auth problem, not empty data.
            raise ConfigEntryAuthFailed(
                f"Cannot read {self._repository}; check the token has access to it"
            )

        nodes = (repository.get("pullRequests") or {}).get("nodes") or []
        wanted = _normalise_author(self._pr_author)

        result: dict[str, RenovatePullRequest] = {}
        drafts = 0
        wrong_author = 0
        for node in nodes:
            if not node:
                continue
            if node.get("isDraft"):
                drafts += 1
                continue
            # An empty author filter matches everything, which is the escape
            # hatch when the pull requests turn out to be authored by something
            # other than the configured account.
            author = ((node.get("author") or {}).get("login")) or ""
            if wanted and _normalise_author(author) != wanted:
                wrong_author += 1
                continue

            pr = _parse(
                number=node["number"],
                title=node.get("title") or "",
                url=node.get("url") or "",
                mergeable=node.get("mergeable") or "",
                body=node.get("body") or "",
            )
            # Two open PRs for one dependency should not fight over an entity;
            # the lower number is the older, so the newer one wins.
            existing = result.get(pr.key)
            if existing is None or pr.number > existing.number:
                result[pr.key] = pr

        _LOGGER.debug(
            "%s: %d open PR(s); %d matched author %r, %d skipped as drafts, "
            "%d by another author",
            self._repository,
            len(nodes),
            len(result),
            self._pr_author,
            drafts,
            wrong_author,
        )
        return result

    async def async_merge_pull_request(self, number: int) -> None:
        """Merge a pull request, raising HomeAssistantError if GitHub refuses."""
        try:
            response = await self._session.put(
                f"{GITHUB_API}/repos/{self._repository}/pulls/{number}/merge",
                headers=self._headers,
                json={"merge_method": self._merge_method},
            )
            body = await response.text()
        except ClientError as err:
            raise HomeAssistantError(f"Error merging PR #{number}: {err}") from err

        if response.status != 200:
            # 405 = not mergeable (conflict, or the merge method is disabled on
            # the repository), 409 = head changed since the SHA was read.
            raise HomeAssistantError(
                f"GitHub refused to merge PR #{number} (HTTP {response.status}): {body}"
            )

        _LOGGER.info("Merged PR #%d in %s", number, self._repository)
