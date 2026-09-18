"""Shared base class for all ebusd-direct entities."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EbusdCoordinator
from .model import FieldDesc

# Product names for known Vaillant scan IDs. Deliberately WITHOUT the heat
# pump models (HMU00/V32): there the scanned model is the coupler, or not
# very descriptive, and the user-facing circuit (wp0/wp1) is more meaningful.
_PRODUCT_NAMES = {
    "CTLV3": "sensoCOMFORT",
    "VR_71": "VR 71",
    "VR_70": "VR 70",
    "VR630": "VR 630",
}


def _device_name(circuit: str, model: str | None) -> str:
    if model and model in _PRODUCT_NAMES:
        return _PRODUCT_NAMES[model]
    return circuit.replace("_", " ").upper()  # "vr_71" -> "VR 71", "wp0" -> "WP0"


# Icon heuristic: UNIT first. Temperatures (°C/K) -> a thermometer variant
# depending on context; otherwise a device icon by name keyword, unit last.
_TEMP_UNITS = {"°C", "K"}

_NONTEMP_KEYWORD: list[tuple[str, str]] = [
    ("name", "mdi:rename"),  # zone names/short labels (Name1/2, Shortname)
    ("pump", "mdi:pump"),
    ("valve", "mdi:pipe-valve"),
    ("compressor", "mdi:heat-pump"),
    ("fan", "mdi:fan"),
    ("curve", "mdi:chart-bell-curve"),
    ("cool", "mdi:snowflake"),
    ("heat", "mdi:radiator"),
    ("mode", "mdi:tune-variant"),
    ("status", "mdi:information-outline"),
    ("pressure", "mdi:gauge"),
    ("energy", "mdi:lightning-bolt"),
    ("power", "mdi:flash"),
    ("time", "mdi:clock-outline"),
]
_ICON_BY_UNIT: dict[str, str] = {
    "%": "mdi:percent",
    "bar": "mdi:gauge",
    "kWh": "mdi:lightning-bolt",
    "Wh": "mdi:lightning-bolt",
    "kW": "mdi:flash",
    "W": "mdi:flash",
    "V": "mdi:flash-triangle",
    "A": "mdi:current-ac",
    "h": "mdi:clock-outline",
    "min": "mdi:clock-outline",
}


def _temp_icon(text: str, unit: str) -> str:
    if "cool" in text:
        return "mdi:snowflake-thermometer"
    if "room" in text:
        return "mdi:home-thermometer"
    if any(k in text for k in ("flow", "water", "dhw", "hwc", "cylinder")):
        return "mdi:thermometer-water"
    if any(k in text for k in ("outside", "outdoor", "ambient", "extern")):
        return "mdi:sun-thermometer"
    if unit == "K":
        return "mdi:thermometer-lines"
    return "mdi:thermometer"


def _icon_for(desc: FieldDesc) -> str | None:
    text = f"{desc.message} {desc.field}".lower()
    unit = desc.unit or ""
    if unit in _TEMP_UNITS:
        return _temp_icon(text, unit)
    for keyword, icon in _NONTEMP_KEYWORD:
        if keyword in text:
            return icon
    return _ICON_BY_UNIT.get(unit)


def build_device_info(coordinator: EbusdCoordinator, circuit: str) -> DeviceInfo:
    """One device per eBUS circuit -- readable name (not "ebusd"), attaches as a child to the bridge."""
    meta = coordinator.device_meta.get(circuit, {})
    info = DeviceInfo(
        identifiers={(DOMAIN, circuit)},
        name=_device_name(circuit, meta.get("model")),
        manufacturer=meta.get("manufacturer", "Vaillant"),
        model=meta.get("model"),
        sw_version=meta.get("sw"),
        hw_version=meta.get("hw"),
    )
    # via_device (identifier tuple) is removed as of HA 2027.8.0 -- resolve
    # the bridge parent device's actual registry ID instead.
    bridge_device = dr.async_get(coordinator.hass).async_get_device(
        identifiers={coordinator.bridge_id}
    )
    if bridge_device is not None:
        info["via_device_id"] = bridge_device.id
    return info


def add_fields_dynamically(
    coordinator: EbusdCoordinator,
    async_add_entities: AddEntitiesCallback,
    matches: Callable[[FieldDesc], bool],
    build: Callable[[FieldDesc], Any],
    always: Callable[[FieldDesc], bool] | None = None,
) -> Callable[[], None]:
    """Create entities as soon as a field has a value for the first time.

    ebusd's cache is empty after a restart and fills up gradually. Checking
    only at setup would permanently miss anything that had no value yet at
    that point -- and the user would have to reload. Conversely, fields
    without a value don't create orphan entities (unpopulated hardware).

    `always`: fields that are created even without a value (e.g. the error
    log -- empty = "no error", should still be visible as a status).

    Returns: an unsubscribe function for the coordinator listener.
    """
    known: set[tuple[str, str, str]] = set()

    @callback
    def _sync() -> None:
        new = []
        for desc in coordinator.fields:
            if desc.key in known or not matches(desc):
                continue
            if coordinator.data.get(desc.key) is None and not (always and always(desc)):
                continue
            known.add(desc.key)
            new.append(build(desc))
        if new:
            async_add_entities(new)

    _sync()
    return coordinator.async_add_listener(_sync)


class EbusdBaseEntity(CoordinatorEntity[EbusdCoordinator]):
    """Binds an entity to a field descriptor + coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EbusdCoordinator, desc: FieldDesc) -> None:
        super().__init__(coordinator)
        self._desc = desc
        self._attr_name = desc.label
        self._attr_icon = _icon_for(desc)
        self._attr_device_info = build_device_info(coordinator, desc.circuit)
        # Passively overheard command fields (e.g. SetMode) are diagnostic and
        # disabled by default -- otherwise they'd flood the devices. The user
        # opts in to what they specifically need (e.g. releasebackup).
        if desc.passive and not desc.writable:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False

    @property
    def _value(self) -> Any:
        return self.coordinator.data.get(self._desc.key)

    @property
    def available(self) -> bool:
        return super().available and self._value is not None
