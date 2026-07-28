"""Update entities for the dependencies Renovate manages."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RenovateConfigEntry
from .const import DOMAIN
from .coordinator import Dependency, RenovateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RenovateConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up an update entity per dependency, adding new ones as they appear."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_add_new() -> None:
        """Add entities for dependencies seen for the first time.

        Entities are never removed here. A dependency whose pull request merged
        still exists -- it is simply up to date -- and removing the entity would
        make it flicker out of the registry on every merge, which Home Assistant
        handles poorly. It reports "up to date" instead.
        """
        if new := set(coordinator.data or {}) - known:
            async_add_entities(
                RenovateUpdateEntity(coordinator, entry, key) for key in sorted(new)
            )
            known.update(new)
            _LOGGER.debug("Added %d new dependency entities", len(new))

    _async_add_new()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new))


class RenovateUpdateEntity(CoordinatorEntity[RenovateCoordinator], UpdateEntity):
    """A dependency Renovate manages, updated by merging its pull request."""

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
    def _dependency(self) -> Dependency | None:
        """Return the dependency backing this entity."""
        return (self.coordinator.data or {}).get(self._key)

    @property
    def name(self) -> str | None:
        """Return the dependency name."""
        return dep.name if (dep := self._dependency) else self._key

    @property
    def title(self) -> str | None:
        """Return the dependency name, shown above the version numbers."""
        return dep.name if (dep := self._dependency) else None

    @property
    def installed_version(self) -> str | None:
        """Return the version currently pinned in the repository."""
        return dep.installed_version if (dep := self._dependency) else None

    @property
    def latest_version(self) -> str | None:
        """Return the pending version, or the installed one when up to date."""
        return dep.latest_version if (dep := self._dependency) else None

    @property
    def release_url(self) -> str | None:
        """Return a link to the pending pull request, if there is one."""
        dep = self._dependency
        return dep.pull_request.url if dep and dep.pull_request else None

    @property
    def release_summary(self) -> str | None:
        """Return a one-line summary of the pending pull request.

        Home Assistant truncates this to 255 characters, so the detail lives in
        release_notes instead.
        """
        dep = self._dependency
        if dep is None or (pull_request := dep.pull_request) is None:
            return None
        summary = f"PR #{pull_request.number}"
        if pull_request.has_conflict:
            summary += " — has merge conflicts"
        return summary

    async def async_release_notes(self) -> str | None:
        """Return the pull request body, rendered in the more-info dialog.

        This is the Renovate PR body verbatim: the change table followed by the
        upstream changelogs Renovate collected for every release between the
        installed and target versions.
        """
        dep = self._dependency
        if dep is None or dep.pull_request is None:
            return None
        return dep.pull_request.body

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Merge the pending pull request."""
        dep = self._dependency
        if dep is None or (pull_request := dep.pull_request) is None:
            raise HomeAssistantError(f"No pending update for {self._key}")

        await self.coordinator.async_merge_pull_request(pull_request.number)
        await self.coordinator.async_request_refresh()
