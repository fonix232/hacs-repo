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
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_VERSION,
    DOMAIN,
    GITHUB_API,
    PR_HISTORY_LIMIT,
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

STORAGE_VERSION = 1

# Two connections in one request. Open pull requests carry their bodies, which
# are large; merged ones are fetched by title only and exist purely to discover
# dependencies that have no pending update, so that every dependency can have a
# permanent entity rather than one that appears and disappears with its PR.
_QUERY = """
query($owner: String!, $name: String!, $limit: Int!, $history: Int!) {
  repository(owner: $owner, name: $name) {
    open: pullRequests(
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
    merged: pullRequests(
      states: MERGED
      first: $history
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      nodes {
        number
        title
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


def _identify(title: str, number: int) -> tuple[str, str]:
    """Return the (key, display name) for the dependency a PR title refers to."""
    if dep := _DEP_RE.search(title):
        name = dep.group(1)
        return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_"), name

    # Grouped or non-docker PRs have no single dependency to key on, so fall
    # back to the pull request itself.
    return f"pr_{number}", (title.split(":", 1)[-1].strip() or title)


@dataclass(slots=True)
class RenovatePullRequest:
    """An open Renovate pull request."""

    number: int
    title: str
    url: str
    mergeable: str
    body: str
    installed_version: str | None
    latest_version: str | None

    @property
    def has_conflict(self) -> bool:
        """Return True when GitHub reports the PR as not mergeable."""
        return self.mergeable == "CONFLICTING"


@dataclass(slots=True)
class Dependency:
    """A dependency Renovate manages, with or without a pending update.

    One of these exists for every dependency ever seen, so its entity is created
    once and then stays put. Home Assistant dislikes entities that come and go,
    and removing one whenever its pull request merged made the registry churn.
    """

    key: str
    name: str
    current_version: str | None
    pull_request: RenovatePullRequest | None = None

    @property
    def installed_version(self) -> str:
        """Return the version believed to be in the repository right now."""
        if self.pull_request is not None:
            return (
                self.pull_request.installed_version or self.current_version or "unknown"
            )
        return self.current_version or "unknown"

    @property
    def latest_version(self) -> str:
        """Return the target version, or the installed one when up to date.

        With no pending pull request these are deliberately equal, which is what
        makes the entity report "up to date" rather than vanishing.
        """
        if self.pull_request is not None:
            return self.pull_request.latest_version or "unknown"
        return self.installed_version


def _parse_open(node: dict) -> tuple[str, str, RenovatePullRequest]:
    """Turn an open pull request node into (key, name, pull request)."""
    title = node.get("title") or ""
    number = node["number"]
    body = node.get("body") or ""

    # Prefer the exact `old` -> `new` pair from Renovate's change table; the
    # title truncates the target version on major bumps.
    if change := _CHANGE_RE.search(body):
        installed, latest = change.group(1), change.group(2)
    else:
        installed = None
        latest = m.group(1) if (m := _TITLE_VERSION_RE.search(title)) else None

    key, name = _identify(title, number)
    return (
        key,
        name,
        RenovatePullRequest(
            number=number,
            title=title,
            url=node.get("url") or "",
            mergeable=node.get("mergeable") or "UNKNOWN",
            body=body,
            installed_version=installed,
            latest_version=latest,
        ),
    )


class RenovateCoordinator(DataUpdateCoordinator[dict[str, Dependency]]):
    """Track the dependencies Renovate manages for a repository."""

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
        # Remembers dependencies across restarts, so entities survive a period
        # with nothing pending and outlive the merged-history window.
        self._store: Store[dict[str, dict[str, str | None]]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._known: dict[str, dict[str, str | None]] = {}

    @property
    def repository(self) -> str:
        """Return the watched repository, as owner/name."""
        return self._repository

    async def async_load_known(self) -> None:
        """Load the remembered dependencies. Call before the first refresh."""
        self._known = await self._store.async_load() or {}
        _LOGGER.debug("Loaded %d remembered dependencies", len(self._known))

    async def async_forget(self) -> None:
        """Drop the remembered dependencies when the entry is removed."""
        await self._store.async_remove()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "home-assistant-renovate-updates",
        }

    def _wanted(self, node: dict) -> bool:
        """Return True when the node was authored by the configured account.

        An empty author filter matches everything, which is the escape hatch
        when the pull requests turn out to be authored by something else.
        """
        if not (wanted := _normalise_author(self._pr_author)):
            return True
        author = ((node.get("author") or {}).get("login")) or ""
        return _normalise_author(author) == wanted

    async def _async_update_data(self) -> dict[str, Dependency]:
        """Fetch open pull requests, and rebuild the dependency list."""
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
                        "history": PR_HISTORY_LIMIT,
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

        known = dict(self._known)

        # Merged pull requests, newest first, tell us which dependencies exist
        # and roughly what version each one is on now. Applied first so an open
        # pull request can overwrite the version with something authoritative.
        merged_nodes = (repository.get("merged") or {}).get("nodes") or []
        for node in merged_nodes:
            if not node or not self._wanted(node):
                continue
            title = node.get("title") or ""
            key, dep_name = _identify(title, node["number"])
            if key.startswith("pr_"):
                # A grouped pull request is not one dependency, so it would only
                # add a permanent entity for something that no longer exists.
                continue
            if key in known:
                continue
            version = m.group(1) if (m := _TITLE_VERSION_RE.search(title)) else None
            known[key] = {"name": dep_name, "version": version}

        open_nodes = (repository.get("open") or {}).get("nodes") or []
        pull_requests: dict[str, RenovatePullRequest] = {}
        drafts = 0
        for node in open_nodes:
            if not node:
                continue
            if node.get("isDraft"):
                drafts += 1
                continue
            if not self._wanted(node):
                continue

            key, dep_name, pull_request = _parse_open(node)
            # Two open PRs for one dependency should not fight over an entity;
            # the lower number is the older, so the newer one wins.
            existing = pull_requests.get(key)
            if existing is not None and pull_request.number <= existing.number:
                continue
            pull_requests[key] = pull_request
            known[key] = {
                "name": dep_name,
                # The PR's "from" side is the current pin, straight from the
                # repository, so it is better than anything inferred.
                "version": pull_request.installed_version
                or known.get(key, {}).get("version"),
            }

        if known != self._known:
            self._known = known
            await self._store.async_save(known)

        result = {
            key: Dependency(
                key=key,
                name=str(entry.get("name") or key),
                current_version=entry.get("version"),
                pull_request=pull_requests.get(key),
            )
            for key, entry in known.items()
        }

        _LOGGER.debug(
            "%s: %d dependencies known, %d with a pending update "
            "(%d open PR(s) seen, %d skipped as drafts, author filter %r)",
            self._repository,
            len(result),
            len(pull_requests),
            len(open_nodes),
            drafts,
            self._pr_author,
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
