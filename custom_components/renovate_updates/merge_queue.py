"""A merge queue for Renovate pull requests.

GitHub's own merge queue only exists on public repositories and paid plans, so
this is a small one of the integration's own. The install button hands a pull
request to it and returns at once; a background worker then merges the queued
pull requests one at a time, in the order they were queued.

Merging one at a time matters because of what happens after each merge:

- GitHub recomputes every other open PR's mergeability lazily, and answers a
  merge call made in that window with 405 "base branch was modified". The
  worker simply looks again a few seconds later.
- Renovate PRs that edit neighbouring lines of the same file conflict with
  each other, so the second of a pair is CONFLICTING the moment the first
  lands. Renovate rebases conflicted PRs by itself; the worker also ticks the
  PR's rebase checkbox to prompt it, then waits for the new head to become
  mergeable and carries on.

The queue is persisted, so a Home Assistant restart resumes it. Progress is
reported on the update entities through `in_progress` and attributes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import (
    QUEUE_BACKOFF_SECONDS,
    QUEUE_ENTRY_TIMEOUT_SECONDS,
    QUEUE_MAX_MERGE_REFUSALS,
)
from .coordinator import MergeRefused, RenovateCoordinator

_LOGGER = logging.getLogger(__name__)

# Per-entry states, surfaced on the entity as `queue_state`.
STATE_QUEUED = "queued"
STATE_MERGING = "merging"
STATE_REBASING = "waiting_for_rebase"


@dataclass(slots=True)
class QueueEntry:
    """One pull request waiting to be merged."""

    number: int
    key: str
    enqueued_at: float
    state: str = STATE_QUEUED
    # Head SHA the rebase checkbox was last ticked for. Renovate unticks it
    # when it rebases, and the new head has a new SHA, so this is what stops
    # the same conflict being reported to Renovate on every pass.
    rebase_requested_for: str | None = None
    # Consecutive passes on which GitHub called the PR mergeable and then
    # refused to merge it.
    refusals: int = 0

    def as_dict(self) -> dict:
        """Return the part of the entry worth keeping across a restart."""
        return {
            "number": self.number,
            "key": self.key,
            "enqueued_at": self.enqueued_at,
            "rebase_requested_for": self.rebase_requested_for,
        }

    @classmethod
    def from_dict(cls, data: dict) -> QueueEntry:
        """Rebuild an entry saved by as_dict()."""
        return cls(
            number=int(data["number"]),
            key=str(data.get("key") or f"pr_{data['number']}"),
            enqueued_at=float(data.get("enqueued_at") or 0),
            rebase_requested_for=data.get("rebase_requested_for"),
        )


class MergeQueue:
    """Merge queued pull requests one at a time in the background."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: RenovateCoordinator,
        store: Store[dict],
    ) -> None:
        """Initialise an empty queue; call async_load() to restore a saved one."""
        self._hass = hass
        self._coordinator = coordinator
        self._store = store
        self._entries: list[QueueEntry] = []
        # Pull requests dropped from the queue, with the reason, so the entity
        # can say why its install did not happen. Cleared when re-queued.
        self._failures: dict[int, str] = {}
        self._wake = asyncio.Event()
        self._now = time.time

    # -- state ---------------------------------------------------------------

    async def async_load(self) -> None:
        """Restore the queue saved before the last restart."""
        data = await self._store.async_load() or {}
        self._entries = [QueueEntry.from_dict(item) for item in data.get("entries", [])]
        if self._entries:
            _LOGGER.info(
                "Resuming merge queue with %d pull request(s): %s",
                len(self._entries),
                ", ".join(f"#{entry.number}" for entry in self._entries),
            )

    async def _async_save(self) -> None:
        await self._store.async_save(
            {"entries": [entry.as_dict() for entry in self._entries]}
        )

    async def async_forget(self) -> None:
        """Drop the saved queue when the entry is removed."""
        await self._store.async_remove()

    def __len__(self) -> int:
        """Return how many pull requests are queued."""
        return len(self._entries)

    @property
    def entries(self) -> tuple[QueueEntry, ...]:
        """Return the queued pull requests, first to merge first."""
        return tuple(self._entries)

    def entry_for(self, number: int) -> QueueEntry | None:
        """Return the queue entry for a pull request, if it is queued."""
        return next((e for e in self._entries if e.number == number), None)

    def position(self, number: int) -> int | None:
        """Return the 1-based queue position of a pull request, if queued."""
        for index, entry in enumerate(self._entries, start=1):
            if entry.number == number:
                return index
        return None

    def failure_for(self, number: int) -> str | None:
        """Return why a pull request was dropped from the queue, if it was."""
        return self._failures.get(number)

    def _notify(self) -> None:
        """Tell the entities the queue changed."""
        self._coordinator.async_update_listeners()

    # -- input ---------------------------------------------------------------

    async def async_enqueue(self, number: int, key: str) -> bool:
        """Queue a pull request. Returns False if it was already queued."""
        if self.entry_for(number) is not None:
            return False
        self._failures.pop(number, None)
        self._entries.append(
            QueueEntry(number=number, key=key, enqueued_at=self._now())
        )
        await self._async_save()
        _LOGGER.info(
            "Queued PR #%d for merging (position %d)", number, len(self._entries)
        )
        self._notify()
        self.async_wake()
        return True

    @callback
    def async_wake(self) -> None:
        """Have the worker look at the queue now rather than after its nap.

        Also registered as a coordinator listener, so a webhook delivery or a
        poll that noticed something changed shortens the wait.
        """
        self._wake.set()

    # -- worker --------------------------------------------------------------

    async def async_run(self) -> None:
        """Process the queue until cancelled."""
        step = 0
        while True:
            if not self._entries:
                await self._wake.wait()
                self._wake.clear()
                step = 0
                continue

            try:
                merged = await self.async_process_once()
            except Exception:  # noqa: BLE001 - the worker must survive anything
                _LOGGER.exception("Merge queue pass failed; will retry")
                merged = False

            # A merge restarts the ladder: the next PR is usually ready within
            # seconds. Otherwise back off, up to the last rung, while Renovate
            # or GitHub does its part.
            step = 0 if merged else min(step + 1, len(QUEUE_BACKOFF_SECONDS) - 1)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), QUEUE_BACKOFF_SECONDS[step])
            except TimeoutError:
                continue
            # Woken early by an enqueue or a refresh: something changed, so go
            # back to looking often.
            step = 0

    async def async_process_once(self) -> bool:
        """Make one pass over the queue. Returns True if a merge happened.

        At most one pull request is merged per pass, because every merge
        invalidates what GitHub told us about the others.
        """
        now = self._now()
        dirty = False  # entries changed: persist
        changed = False  # anything an entity shows changed: notify

        for entry in list(self._entries):
            if now - entry.enqueued_at > QUEUE_ENTRY_TIMEOUT_SECONDS:
                self._fail(
                    entry,
                    "gave up: not mergeable within "
                    f"{QUEUE_ENTRY_TIMEOUT_SECONDS // 3600} hours",
                )
                dirty = changed = True
                continue

            try:
                status = await self._coordinator.async_fetch_pull_request(entry.number)
            except ConfigEntryAuthFailed as err:
                # The coordinator's own refresh raises the reauth prompt; the
                # queue just waits for the token to be fixed.
                _LOGGER.error("Merge queue paused: %s", err)
                break
            except (UpdateFailed, HomeAssistantError) as err:
                _LOGGER.warning(
                    "Merge queue could not read PR #%d: %s", entry.number, err
                )
                break

            if status is None or status.state == "MERGED":
                # Merged by hand, or deleted: nothing left for the queue to do.
                _LOGGER.info(
                    "PR #%d is no longer open; dropping from queue", entry.number
                )
                self._entries.remove(entry)
                dirty = changed = True
                continue
            if status.state == "CLOSED":
                self._fail(entry, "closed without merging")
                dirty = changed = True
                continue
            if status.is_draft:
                self._fail(entry, "marked as a draft")
                dirty = changed = True
                continue

            if status.mergeable == "MERGEABLE":
                changed |= self._set_state(entry, STATE_MERGING)
                try:
                    await self._coordinator.async_merge_pull_request(entry.number)
                except MergeRefused as err:
                    entry.refusals += 1
                    if not err.transient or entry.refusals >= QUEUE_MAX_MERGE_REFUSALS:
                        self._fail(entry, str(err))
                        dirty = True
                    else:
                        # Almost always the base branch moved a moment ago and
                        # GitHub has not caught up; next pass will know more.
                        _LOGGER.debug("PR #%d not merged yet: %s", entry.number, err)
                        self._set_state(entry, STATE_QUEUED)
                    changed = True
                    continue
                except HomeAssistantError as err:
                    _LOGGER.warning(
                        "Merge queue could not merge PR #%d: %s", entry.number, err
                    )
                    self._set_state(entry, STATE_QUEUED)
                    changed = True
                    break

                self._entries.remove(entry)
                await self._async_save()
                self._notify()
                # Refresh the entities so the merged dependency reads up to date.
                await self._coordinator.async_request_refresh()
                return True

            entry.refusals = 0
            if status.mergeable == "CONFLICTING":
                changed |= self._set_state(entry, STATE_REBASING)
                if entry.rebase_requested_for != status.head_sha:
                    try:
                        asked = await self._coordinator.async_request_rebase(
                            entry.number, status.body
                        )
                    except HomeAssistantError as err:
                        _LOGGER.warning(
                            "Could not ask Renovate to rebase PR #%d: %s",
                            entry.number,
                            err,
                        )
                        asked = False
                    if not asked:
                        # No checkbox to tick, or the edit failed. Renovate
                        # rebases conflicted PRs on its own anyway; just wait.
                        _LOGGER.info(
                            "PR #%d conflicts; waiting for Renovate to rebase it",
                            entry.number,
                        )
                    entry.rebase_requested_for = status.head_sha
                    dirty = changed = True
            else:
                # UNKNOWN: GitHub is still working it out, and our query is what
                # prompted it to start. Ask again next pass.
                changed |= self._set_state(entry, STATE_QUEUED)

        if dirty:
            await self._async_save()
        if changed:
            self._notify()
        return False

    def _set_state(self, entry: QueueEntry, state: str) -> bool:
        """Update an entry's state, returning True if it changed."""
        if entry.state == state:
            return False
        entry.state = state
        return True

    def _fail(self, entry: QueueEntry, reason: str) -> None:
        """Drop an entry from the queue and remember why."""
        _LOGGER.warning(
            "Dropping PR #%d from the merge queue: %s", entry.number, reason
        )
        self._entries.remove(entry)
        self._failures[entry.number] = reason
