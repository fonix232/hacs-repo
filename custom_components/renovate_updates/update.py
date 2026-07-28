"""Update entities for open Renovate pull requests."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.update import (
    DOMAIN as UPDATE_DOMAIN,
)
from homeassistant.components.update import (
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RenovateConfigEntry
from .const import DOMAIN
from .coordinator import RenovateCoordinator, RenovatePullRequest

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RenovateConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up update entities, adding and removing them as PRs come and go."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_sync_entities() -> None:
        """Reconcile the entity list with the pull requests currently open."""
        current = set(coordinator.data or {})

        if added := current - known:
            async_add_entities(
                RenovateUpdateEntity(coordinator, entry, key) for key in sorted(added)
            )
            known.update(added)

        # A merged or closed PR means there is nothing left to update, so drop
        # the entity rather than leaving a stale row in the Updates card. The
        # unique_id is keyed on the dependency, so the next PR for the same
        # image reuses the same entity_id.
        if removed := known - current:
            registry = er.async_get(hass)
            for key in removed:
                unique_id = f"{entry.entry_id}_{key}"
                if entity_id := registry.async_get_entity_id(
                    UPDATE_DOMAIN, DOMAIN, unique_id
                ):
                    _LOGGER.debug("Removing %s; its pull request is closed", entity_id)
                    registry.async_remove(entity_id)
            known.difference_update(removed)

    _async_sync_entities()
    entry.async_on_unload(coordinator.async_add_listener(_async_sync_entities))


class RenovateUpdateEntity(CoordinatorEntity[RenovateCoordinator], UpdateEntity):
    """An open Renovate pull request, presented as an available update."""

    _attr_has_entity_name = True
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.RELEASE_NOTES
    )

    def __init__(
        self,
        coordinator: RenovateCoordinator,
        entry: RenovateConfigEntry,
        key: str,
    ) -> None:
        """Initialise the entity for one dependency."""
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Renovate ({coordinator.repository})",
            manufacturer="Renovate",
            model=coordinator.repository,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=f"https://github.com/{coordinator.repository}/pulls",
        )

    @property
    def _pr(self) -> RenovatePullRequest | None:
        """Return the pull request backing this entity, if still open."""
        return (self.coordinator.data or {}).get(self._key)

    @property
    def available(self) -> bool:
        """Return True while the pull request is still open."""
        return super().available and self._pr is not None

    @property
    def name(self) -> str | None:
        """Return the dependency name."""
        return self._pr.name if self._pr else self._key

    @property
    def title(self) -> str | None:
        """Return the dependency name, shown above the version numbers."""
        return self._pr.name if self._pr else None

    @property
    def installed_version(self) -> str | None:
        """Return the version currently pinned in the repository."""
        return self._pr.installed_version if self._pr else None

    @property
    def latest_version(self) -> str | None:
        """Return the version the pull request would move to."""
        return self._pr.latest_version if self._pr else None

    @property
    def release_url(self) -> str | None:
        """Return a link to the pull request."""
        return self._pr.url if self._pr else None

    @property
    def release_summary(self) -> str | None:
        """Return a one-line summary.

        Home Assistant truncates this to 255 characters, so the detail lives in
        release_notes instead.
        """
        if (pr := self._pr) is None:
            return None
        summary = f"PR #{pr.number}"
        if pr.has_conflict:
            summary += " — has merge conflicts"
        return summary

    async def async_release_notes(self) -> str | None:
        """Return the pull request body, rendered in the more-info dialog.

        This is the Renovate PR body verbatim: the change table followed by the
        upstream changelogs Renovate collected for every release between the
        installed and target versions.
        """
        return self._pr.body if self._pr else None

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Merge the pull request."""
        if (pr := self._pr) is None:
            raise HomeAssistantError("The pull request is no longer open")

        await self.coordinator.async_merge_pull_request(pr.number)
        await self.coordinator.async_request_refresh()
