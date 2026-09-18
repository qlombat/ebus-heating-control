"""Switch platform: writable on/off fields (pure on/off enums)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import EbusdError
from .const import DOMAIN
from .coordinator import EbusdCoordinator
from .entity import EbusdBaseEntity, add_fields_dynamically, build_device_info
from .model import FieldDesc, bool_tokens, is_binary, value_is_on

# Curated DHW boost from the HwcSFMode special function (one-off load).
BOOST_MESSAGE = "HwcSFMode"
BOOST_ON = "load"
BOOST_OFF = "auto"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EbusdCoordinator = hass.data[DOMAIN][entry.entry_id]
    entry.async_on_unload(
        add_fields_dynamically(
            coordinator,
            async_add_entities,
            lambda d: d.writable and is_binary(d) and coordinator.included(d),
            lambda d: EbusdSwitch(coordinator, d),
        )
    )
    # Curated overlay: DHW boost (HwcSFMode knows the value "load")
    seen: set[str] = set()
    boosts = []
    for d in coordinator.fields:
        if (
            d.message == BOOST_MESSAGE
            and d.writable
            and d.values
            and d.circuit not in seen
            and any(str(v).lower() == BOOST_ON for v in d.values.values())
        ):
            seen.add(d.circuit)
            boosts.append(EbusdBoostSwitch(coordinator, d))
    async_add_entities(boosts)


class EbusdSwitch(EbusdBaseEntity, SwitchEntity):
    def __init__(self, coordinator: EbusdCoordinator, desc: FieldDesc) -> None:
        super().__init__(coordinator, desc)
        self._attr_unique_id = f"{DOMAIN}_{desc.uid}_set"
        self._on_token, self._off_token = bool_tokens(desc)

    @property
    def is_on(self) -> bool | None:
        return value_is_on(self._value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(self._on_token)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(self._off_token)

    async def _write(self, token: str) -> None:
        await self.coordinator.client.write(
            self._desc.circuit, self._desc.message, token
        )
        # Read back right after writing (unpolled values would stay empty otherwise).
        try:
            await self.coordinator.client.read(
                self._desc.circuit, self._desc.message
            )
        except EbusdError:
            pass
        await self.coordinator.async_request_refresh()


class EbusdBoostSwitch(CoordinatorEntity[EbusdCoordinator], SwitchEntity):
    """DHW boost: HwcSFMode = load (on) / auto (off)."""

    _attr_has_entity_name = True
    _attr_name = "DHW boost"
    _attr_icon = "mdi:water-plus"

    def __init__(self, coordinator: EbusdCoordinator, desc: FieldDesc) -> None:
        super().__init__(coordinator)
        self._desc = desc
        names = {str(v).lower(): v for v in (desc.values or {}).values()}
        self._on_token = names.get(BOOST_ON, BOOST_ON)
        self._off_token = names.get(BOOST_OFF, BOOST_OFF)
        self._attr_unique_id = f"{DOMAIN}_{desc.circuit}_hwc_boost".lower()
        self._attr_device_info = build_device_info(coordinator, desc.circuit)

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.get(self._desc.key)
        if value is None:
            return None
        return str(value).lower() == BOOST_ON

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(self._on_token)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(self._off_token)

    async def _write(self, token: str) -> None:
        try:
            await self.coordinator.client.write(
                self._desc.circuit, self._desc.message, token
            )
        except EbusdError as err:
            raise HomeAssistantError(f"Boost write failed: {err}") from err
        try:
            await self.coordinator.client.read(
                self._desc.circuit, self._desc.message
            )
        except EbusdError:
            pass
        await self.coordinator.async_request_refresh()
