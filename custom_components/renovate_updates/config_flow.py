"""Config and options flow for Renovate Updates."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.components import webhook
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_TOKEN, CONF_WEBHOOK_ID
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    API_VERSION,
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
    GITHUB_API,
    MERGE_METHODS,
    UPDATE_METHODS,
)

_LOGGER = logging.getLogger(__name__)

# Deliberately reads pullRequests, not just repository metadata. A fine-grained
# token with only Metadata and Contents can resolve nameWithOwner perfectly well
# but is FORBIDDEN on pullRequests, so probing metadata alone would accept a
# token that cannot do the one thing this integration exists to do.
_PROBE = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    viewerPermission
    pullRequests(states: OPEN, first: 1) {
      totalCount
    }
  }
}
"""


def _permission_error(errors: list[dict]) -> bool:
    """Return True when GitHub refused a field for lack of a token permission.

    GraphQL answers with HTTP 200 and nulls the parent object, so the refusal
    only shows up as a FORBIDDEN entry in the errors array.
    """
    return any(
        error.get("type") in ("FORBIDDEN", "UNAUTHORIZED")
        or "not accessible by personal access token" in (error.get("message") or "")
        for error in errors
    )


async def _async_validate(hass, repository: str, token: str) -> str | None:
    """Check the token can read the repository's pull requests.

    Returns a translation key for the error, or None when everything is usable.
    """
    owner, _, name = repository.partition("/")
    if not owner or not name:
        return "invalid_repository"

    session = async_get_clientsession(hass)
    try:
        response = await session.post(
            f"{GITHUB_API}/graphql",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "home-assistant-renovate-updates",
            },
            json={"query": _PROBE, "variables": {"owner": owner, "name": name}},
        )
        if response.status in (401, 403):
            return "invalid_auth"
        if response.status != 200:
            return "cannot_connect"
        payload = await response.json()
    except ClientError:
        return "cannot_connect"

    if errors := payload.get("errors"):
        if _permission_error(errors):
            return "missing_pr_permission"
        _LOGGER.debug("Validation failed for %s: %s", repository, errors)
        return "invalid_repository"

    # Null without an error means the token authenticated but cannot see this
    # repository at all; accepting it would produce a silently empty integration.
    if ((payload.get("data") or {}).get("repository")) is None:
        return "invalid_repository"
    return None


def _merge_method_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=MERGE_METHODS,
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="merge_method",
        )
    )


def _update_method_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=UPDATE_METHODS,
            mode=SelectSelectorMode.LIST,
            translation_key="update_method",
        )
    )


def _minutes_selector(maximum: int) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=0,
            max=maximum,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="min",
        )
    )


class RenovateConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the repository and token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            repository = (
                user_input[CONF_REPOSITORY].strip().removeprefix("https://github.com/")
            )
            repository = repository.strip("/")
            await self.async_set_unique_id(repository.lower())
            self._abort_if_unique_id_configured()

            if error := await _async_validate(
                self.hass, repository, user_input[CONF_TOKEN]
            ):
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title=repository,
                    data={
                        CONF_REPOSITORY: repository,
                        CONF_TOKEN: user_input[CONF_TOKEN],
                        # Always allocated, so switching to webhook mode later
                        # does not require re-entering the token.
                        CONF_WEBHOOK_ID: webhook.async_generate_id(),
                    },
                    options={
                        CONF_PR_AUTHOR: user_input.get(CONF_PR_AUTHOR, ""),
                        CONF_MERGE_METHOD: user_input[CONF_MERGE_METHOD],
                        CONF_UPDATE_METHOD: DEFAULT_UPDATE_METHOD,
                        CONF_SCAN_INTERVAL_MINUTES: DEFAULT_SCAN_INTERVAL_MINUTES,
                        CONF_FALLBACK_INTERVAL_MINUTES: (
                            DEFAULT_FALLBACK_INTERVAL_MINUTES
                        ),
                    },
                )

        suggested = user_input or {}
        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_REPOSITORY, default=suggested.get(CONF_REPOSITORY, "")
                    ): TextSelector(),
                    vol.Required(CONF_TOKEN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Optional(
                        CONF_PR_AUTHOR,
                        default=suggested.get(CONF_PR_AUTHOR, DEFAULT_PR_AUTHOR),
                    ): TextSelector(),
                    vol.Required(
                        CONF_MERGE_METHOD,
                        default=suggested.get(CONF_MERGE_METHOD, DEFAULT_MERGE_METHOD),
                    ): _merge_method_selector(),
                }
            ),
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a token that stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take a replacement token, keeping the rest of the entry intact."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            if error := await _async_validate(
                self.hass, entry.data[CONF_REPOSITORY], user_input[CONF_TOKEN]
            ):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: user_input[CONF_TOKEN]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            errors=errors,
            description_placeholders={"repository": entry.data[CONF_REPOSITORY]},
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TOKEN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> RenovateOptionsFlow:
        """Return the options flow."""
        return RenovateOptionsFlow()


class RenovateOptionsFlow(OptionsFlow):
    """Let the user switch between polling and webhook, and tune the rest."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the options."""
        entry = self.config_entry
        options = entry.options

        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_PR_AUTHOR: user_input.get(CONF_PR_AUTHOR, ""),
                    CONF_MERGE_METHOD: user_input[CONF_MERGE_METHOD],
                    CONF_UPDATE_METHOD: user_input[CONF_UPDATE_METHOD],
                    CONF_SCAN_INTERVAL_MINUTES: int(
                        user_input[CONF_SCAN_INTERVAL_MINUTES]
                    ),
                    CONF_FALLBACK_INTERVAL_MINUTES: int(
                        user_input[CONF_FALLBACK_INTERVAL_MINUTES]
                    ),
                }
            )

        # Surfaced in the form description so the URL can be pasted straight
        # into the repository's webhook settings.
        webhook_url = webhook.async_generate_url(self.hass, entry.data[CONF_WEBHOOK_ID])

        return self.async_show_form(
            step_id="init",
            description_placeholders={
                "webhook_url": webhook_url,
                "repository": entry.data[CONF_REPOSITORY],
            },
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_UPDATE_METHOD,
                        default=options.get(CONF_UPDATE_METHOD, DEFAULT_UPDATE_METHOD),
                    ): _update_method_selector(),
                    vol.Required(
                        CONF_SCAN_INTERVAL_MINUTES,
                        default=options.get(
                            CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES
                        ),
                    ): _minutes_selector(1440),
                    vol.Required(
                        CONF_FALLBACK_INTERVAL_MINUTES,
                        default=options.get(
                            CONF_FALLBACK_INTERVAL_MINUTES,
                            DEFAULT_FALLBACK_INTERVAL_MINUTES,
                        ),
                    ): _minutes_selector(1440),
                    vol.Optional(
                        CONF_PR_AUTHOR,
                        default=options.get(CONF_PR_AUTHOR, DEFAULT_PR_AUTHOR),
                    ): TextSelector(),
                    vol.Required(
                        CONF_MERGE_METHOD,
                        default=options.get(CONF_MERGE_METHOD, DEFAULT_MERGE_METHOD),
                    ): _merge_method_selector(),
                }
            ),
        )
