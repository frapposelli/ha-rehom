"""Config flow for Rehom: user, zeroconf, dhcp, reauth, reconfigure, options."""

from __future__ import annotations

from collections.abc import Mapping
from ipaddress import ip_address
import logging
from typing import Any, Self

from aiorehom import RehomAuthenticationError, RehomError
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
import voluptuous as vol

from . import api
from .const import (
    CONF_ENABLE_CONTROL,
    CONF_TEMPORARY_COMFORT_DURATION,
    DEFAULT_ENABLE_CONTROL,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TEMPORARY_COMFORT_DURATION,
    DEFAULT_TITLE,
    DOMAIN,
    MAX_TEMPORARY_COMFORT_DURATION,
    MIN_TEMPORARY_COMFORT_DURATION,
    TEMPORARY_COMFORT_DURATION_STEP,
)

_LOGGER = logging.getLogger(__name__)

_PORT_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(min=1, max=65535, step=1, mode=selector.NumberSelectorMode.BOX)
)
_PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)
_USERNAME_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT, autocomplete="username")
)


def _host_schema(host: str, port: int) -> dict[vol.Marker, Any]:
    return {
        vol.Required(CONF_HOST, default=host): selector.TextSelector(),
        vol.Required(CONF_PORT, default=port): _PORT_SELECTOR,
    }


def _credentials_schema(username: str | None = None) -> dict[vol.Marker, Any]:
    user_key = (
        vol.Required(CONF_USERNAME)
        if username is None
        else vol.Required(CONF_USERNAME, default=username)
    )
    return {user_key: _USERNAME_SELECTOR, vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR}


def _is_ip(host: str) -> bool:
    try:
        ip_address(host)
    except ValueError:
        return False
    return True


def _address_updates(entry: ConfigEntry | None, updates: dict[str, Any]) -> dict[str, Any] | None:
    """``updates`` for an entry configured by IP address; ``None`` otherwise.

    A configured host name (``rehomserver.local``) is never replaced by a
    discovered address; an ignored entry has no host and is left alone too.
    """
    if entry is not None and _is_ip(entry.data.get(CONF_HOST, "")):
        return updates
    return None


class RehomConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Rehom."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialise the discovery state."""
        self._discovered_host: str = ""
        self._discovered_port: int = DEFAULT_PORT
        self._discovered_mac: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> RehomOptionsFlow:
        """Options: "Enable control" and the default temporary-comfort duration."""
        return RehomOptionsFlow()

    def is_matching(self, other_flow: Self) -> bool:
        """Discovery flows for the same host are the same controller."""
        return bool(self._discovered_host) and other_flow._discovered_host == self._discovered_host

    async def _async_validate(
        self, data: Mapping[str, Any]
    ) -> tuple[api.ControllerInfo | None, dict[str, str]]:
        """Probe the controller; returns (info, errors)."""
        errors: dict[str, str] = {}
        try:
            info = await api.async_probe(self.hass, data)
        except ValueError:
            errors[CONF_HOST] = "invalid_host"
        except RehomAuthenticationError:
            errors["base"] = "invalid_auth"
        except RehomError:
            errors["base"] = "cannot_connect"
        except api.MissingMacError:
            errors["base"] = "missing_mac"
        except Exception:
            _LOGGER.exception("Unexpected error while validating the Rehom controller")
            errors["base"] = "unknown"
        else:
            return info, errors
        return None, errors

    # -- user ----------------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manual setup: host, port, username, password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**user_input, CONF_PORT: int(user_input[CONF_PORT])}
            info, errors = await self._async_validate(data)
            if info is not None:
                await self.async_set_unique_id(info.mac)
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: data[CONF_HOST], CONF_PORT: data[CONF_PORT]}
                )
                return self.async_create_entry(title=DEFAULT_TITLE, data=data)
        suggested = user_input or {}
        schema = vol.Schema(
            {
                **_host_schema(
                    suggested.get(CONF_HOST, DEFAULT_HOST),
                    int(suggested.get(CONF_PORT, DEFAULT_PORT)),
                ),
                **_credentials_schema(suggested.get(CONF_USERNAME)),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    # -- discovery -----------------------------------------------------------------------

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """``rehom._http._tcp.local.``: no id before login, so dedupe by host.

        The mDNS host name is the flow's provisional unique id, so the user can
        ignore the discovery (HA needs a unique id for that) and repeated
        announcements of the same controller do not stack up.  The entry itself
        is keyed by the controller's MAC, read after login (``discovery_confirm``).
        """
        host = discovery_info.host
        hostname = discovery_info.hostname.rstrip(".")
        await self.async_set_unique_id((hostname or host).lower())
        self._abort_if_unique_id_configured()  # an ignored discovery stays ignored
        self._async_abort_entries_match({CONF_HOST: host})
        self._async_abort_entries_match({CONF_HOST: hostname})
        self._discovered_host = host
        self._discovered_port = discovery_info.port or DEFAULT_PORT
        if self.hass.config_entries.flow.async_has_matching_flow(self):
            return self.async_abort(reason="already_in_progress")
        self.context["title_placeholders"] = {"name": f"Rehom ({host})"}
        return await self.async_step_discovery_confirm()

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        """``rehomserver*``: the lease MAC is probably the controller's MAC."""
        mac = format_mac(discovery_info.macaddress)
        entry = await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured(
            updates=_address_updates(entry, {CONF_HOST: discovery_info.ip})
        )
        self._async_abort_entries_match({CONF_HOST: discovery_info.ip})
        self._discovered_host = discovery_info.ip
        self._discovered_mac = mac
        if self.hass.config_entries.flow.async_has_matching_flow(self):
            return self.async_abort(reason="already_in_progress")
        self.context["title_placeholders"] = {"name": f"Rehom ({discovery_info.ip})"}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the credentials of a discovered controller."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                CONF_HOST: self._discovered_host,
                CONF_PORT: self._discovered_port,
                **user_input,
            }
            info, errors = await self._async_validate(data)
            if info is not None:
                # The controller's own MAC wins over the DHCP lease MAC.
                entry = await self.async_set_unique_id(info.mac, raise_on_progress=False)
                # discovery-update-info: the probe just proved this address reaches
                # the configured controller, so an entry kept by IP follows it.
                self._abort_if_unique_id_configured(
                    updates=_address_updates(
                        entry, {CONF_HOST: data[CONF_HOST], CONF_PORT: data[CONF_PORT]}
                    )
                )
                return self.async_create_entry(title=DEFAULT_TITLE, data=data)
        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=vol.Schema(_credentials_schema()),
            errors=errors,
            description_placeholders={"host": self._discovered_host},
        )

    # -- reauth -----------------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Started by ConfigEntryAuthFailed (the login was refused)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for new credentials; keep host and port."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            info, errors = await self._async_validate({**entry.data, **user_input})
            if info is not None:
                await self.async_set_unique_id(info.mac)
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(_credentials_schema(entry.data.get(CONF_USERNAME))),
            errors=errors,
            description_placeholders={"host": entry.data[CONF_HOST]},
        )

    # -- reconfigure --------------------------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change host and port; the controller must stay the same (MAC)."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            updates = {CONF_HOST: user_input[CONF_HOST], CONF_PORT: int(user_input[CONF_PORT])}
            info, errors = await self._async_validate({**entry.data, **updates})
            if info is not None:
                await self.async_set_unique_id(info.mac)
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(entry, data_updates=updates)
        current = user_input or entry.data
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(_host_schema(current[CONF_HOST], int(current[CONF_PORT]))),
            errors=errors,
        )


class RehomOptionsFlow(OptionsFlowWithReload):
    """Options: "Enable control" (off by default) and the temporary-comfort duration.

    Saving reloads the entry (``OptionsFlowWithReload``), so a changed
    "Enable control" rebuilds the client with or without writes.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Single form."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        # Only an exact True shows as on, as only an exact True enables control.
        enable_control = options.get(CONF_ENABLE_CONTROL, DEFAULT_ENABLE_CONTROL) is True
        duration = options.get(CONF_TEMPORARY_COMFORT_DURATION, DEFAULT_TEMPORARY_COMFORT_DURATION)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ENABLE_CONTROL, default=enable_control
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_TEMPORARY_COMFORT_DURATION, default=duration
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_TEMPORARY_COMFORT_DURATION,
                        max=MAX_TEMPORARY_COMFORT_DURATION,
                        step=TEMPORARY_COMFORT_DURATION_STEP,
                        unit_of_measurement="h",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
