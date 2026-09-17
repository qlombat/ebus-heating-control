"""Config-Flow (Host + TCP/HTTP-Port) + Options-Flow (Poll-Intervall + Ausschluss)."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import network
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import EbusdClient, EbusdError
from .const import (
    CONF_BOILER_CURVE_SLOPE,
    CONF_BOILER_FLOW_MAX,
    CONF_BOILER_FLOW_MIN,
    CONF_BOILER_HYSTERESIS,
    CONF_BOILER_KI,
    CONF_BOILER_KP,
    CONF_BOILER_OUTDOOR_SENSOR,
    CONF_BOILER_ROOM_SENSOR,
    CONF_BOILER_WRITE_INTERVAL,
    CONF_EXCLUDE,
    CONF_FAST,
    CONF_HOST,
    CONF_HTTP_PORT,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    DEFAULT_BOILER_CURVE_SLOPE,
    DEFAULT_BOILER_FLOW_MAX,
    DEFAULT_BOILER_FLOW_MIN,
    DEFAULT_BOILER_HYSTERESIS,
    DEFAULT_BOILER_KI,
    DEFAULT_BOILER_KP,
    DEFAULT_BOILER_WRITE_INTERVAL,
    DEFAULT_EXCLUDE,
    DEFAULT_FAST,
    DEFAULT_HTTP_PORT,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)


class EbusdConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            client = EbusdClient(
                user_input[CONF_HOST],
                user_input[CONF_PORT],
                user_input[CONF_HTTP_PORT],
                async_get_clientsession(self.hass),
            )
            try:
                await client.test()
            except EbusdError:
                errors["base"] = "cannot_connect"

            if not errors:
                await self.async_set_unique_id(
                    f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_HOST], data=user_input
                )

        # Host mit der HA-Host-IP vorbelegen (ebusd läuft i. d. R. lokal als Add-on).
        if user_input and user_input.get(CONF_HOST):
            default_host = user_input[CONF_HOST]
        else:
            try:
                default_host = await network.async_get_source_ip(
                    self.hass, target_ip=network.MDNS_TARGET_IP
                )
            except HomeAssistantError:
                default_host = ""

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=default_host): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Required(CONF_HTTP_PORT, default=DEFAULT_HTTP_PORT): int,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return EbusdOptionsFlow(config_entry)


class EbusdOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self._entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): int,
                vol.Optional(
                    CONF_EXCLUDE,
                    default=opts.get(CONF_EXCLUDE, DEFAULT_EXCLUDE),
                ): str,
                vol.Optional(
                    CONF_FAST,
                    default=opts.get(CONF_FAST, DEFAULT_FAST),
                ): str,
                # Kessel-Modulationsregelung: nur aktiv, wenn beide Sensoren
                # gesetzt sind (siehe climate.py) -- deshalb ohne Default, damit
                # sie leer bleiben (und der Nutzer sie wieder leeren kann).
                vol.Optional(
                    CONF_BOILER_ROOM_SENSOR,
                    description={"suggested_value": opts.get(CONF_BOILER_ROOM_SENSOR)},
                ): selector.selector(
                    {"entity": {"domain": "sensor", "device_class": "temperature"}}
                ),
                vol.Optional(
                    CONF_BOILER_OUTDOOR_SENSOR,
                    description={"suggested_value": opts.get(CONF_BOILER_OUTDOOR_SENSOR)},
                ): selector.selector(
                    {"entity": {"domain": "sensor", "device_class": "temperature"}}
                ),
                vol.Optional(
                    CONF_BOILER_CURVE_SLOPE,
                    default=opts.get(CONF_BOILER_CURVE_SLOPE, DEFAULT_BOILER_CURVE_SLOPE),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_BOILER_KP,
                    default=opts.get(CONF_BOILER_KP, DEFAULT_BOILER_KP),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_BOILER_KI,
                    default=opts.get(CONF_BOILER_KI, DEFAULT_BOILER_KI),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_BOILER_FLOW_MIN,
                    default=opts.get(CONF_BOILER_FLOW_MIN, DEFAULT_BOILER_FLOW_MIN),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_BOILER_FLOW_MAX,
                    default=opts.get(CONF_BOILER_FLOW_MAX, DEFAULT_BOILER_FLOW_MAX),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_BOILER_HYSTERESIS,
                    default=opts.get(CONF_BOILER_HYSTERESIS, DEFAULT_BOILER_HYSTERESIS),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_BOILER_WRITE_INTERVAL,
                    default=opts.get(CONF_BOILER_WRITE_INTERVAL, DEFAULT_BOILER_WRITE_INTERVAL),
                ): int,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
