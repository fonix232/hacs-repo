"""The Renovate Updates integration.

Surfaces open Renovate pull requests as Home Assistant update entities, so a
dependency bump can be reviewed and merged without opening GitHub.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from aiohttp.web import Request, Response
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, CONF_WEBHOOK_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_FALLBACK_INTERVAL_MINUTES,
    CONF_MERGE_METHOD,
    CONF_PR_AUTHOR,
    CONF_REPOSITORY,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_UPDATE_METHOD,
    DEFAULT_FALLBACK_INTERVAL_MINUTES,
    DEFAULT_MERGE_METHOD,
    DEFAULT_PR_AUTHOR,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DEFAULT_UPDATE_METHOD,
    DOMAIN,
    UPDATE_METHOD_WEBHOOK,
)
from .coordinator import RenovateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.UPDATE]

RenovateConfigEntry = ConfigEntry[RenovateCoordinator]


def _option(entry: ConfigEntry, key: str, default):
    """Read an option, falling back to the value captured at setup time."""
    return entry.options.get(key, entry.data.get(key, default))


def _update_interval(entry: ConfigEntry) -> timedelta | None:
    """Work out the polling interval for the configured update method.

    In webhook mode this is a slow backstop rather than the primary trigger, so
    a dropped delivery cannot leave the list stale forever. Zero disables it.
    """
    if (
        _option(entry, CONF_UPDATE_METHOD, DEFAULT_UPDATE_METHOD)
        == UPDATE_METHOD_WEBHOOK
    ):
        minutes = _option(
            entry, CONF_FALLBACK_INTERVAL_MINUTES, DEFAULT_FALLBACK_INTERVAL_MINUTES
        )
        return timedelta(minutes=minutes) if minutes else None

    return timedelta(
        minutes=_option(
            entry, CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES
        )
    )


async def async_setup_entry(hass: HomeAssistant, entry: RenovateConfigEntry) -> bool:
    """Set up Renovate Updates from a config entry."""
    coordinator = RenovateCoordinator(
        hass,
        entry,
        async_get_clientsession(hass),
        repository=entry.data[CONF_REPOSITORY],
        token=entry.data[CONF_TOKEN],
        pr_author=_option(entry, CONF_PR_AUTHOR, DEFAULT_PR_AUTHOR),
        merge_method=_option(entry, CONF_MERGE_METHOD, DEFAULT_MERGE_METHOD),
        update_interval=_update_interval(entry),
    )

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    if (
        _option(entry, CONF_UPDATE_METHOD, DEFAULT_UPDATE_METHOD)
        == UPDATE_METHOD_WEBHOOK
    ):
        _async_register_webhook(hass, entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


def _async_register_webhook(
    hass: HomeAssistant, entry: RenovateConfigEntry, coordinator: RenovateCoordinator
) -> None:
    """Register the GitHub webhook endpoint for this entry."""

    async def _handle(
        hass: HomeAssistant, webhook_id: str, request: Request
    ) -> Response:
        """Handle a GitHub webhook delivery.

        The payload is deliberately never read. A delivery is only a hint that
        something changed; the authenticated API call that follows is the single
        source of truth. That means a forged request can do nothing beyond
        causing one extra fetch, so the endpoint needs no signature checking.

        The coordinator's request debouncer coalesces bursts, so the flurry of
        events GitHub sends when a PR opens results in one refresh.
        """
        _LOGGER.debug("Webhook ping received; refreshing")
        await coordinator.async_request_refresh()
        return Response(status=200)

    webhook_id = entry.data[CONF_WEBHOOK_ID]
    webhook.async_register(
        hass,
        DOMAIN,
        f"Renovate Updates ({coordinator.repository})",
        webhook_id,
        _handle,
        allowed_methods=["POST"],
        local_only=False,
    )
    entry.async_on_unload(lambda: webhook.async_unregister(hass, webhook_id))


async def _async_reload_entry(hass: HomeAssistant, entry: RenovateConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: RenovateConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
