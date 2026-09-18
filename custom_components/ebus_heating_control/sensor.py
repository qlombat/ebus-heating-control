"""Sensor platform: readable (non-writable) fields, one entity per field."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EbusdCoordinator
from .entity import EbusdBaseEntity, add_fields_dynamically
from .model import FieldDesc, is_binary, is_error_status, writable_control

# Bridge diagnostics from ebusd's global section.
# (key, name, unit, icon, state_class, enabled by default)
_M = SensorStateClass.MEASUREMENT
_GLOBAL_SENSORS: list[tuple] = [
    ("symbolrate", "Symbol rate", "Sym/s", "mdi:speedometer", _M, True),
    ("maxsymbolrate", "Max. symbol rate", "Sym/s", "mdi:speedometer-medium", _M, True),
    ("reconnects", "Reconnects", None, "mdi:connection", SensorStateClass.TOTAL_INCREASING, True),
    ("masters", "Masters on bus", None, "mdi:sitemap-outline", _M, True),
    ("qq", "ebusd address (QQ)", None, "mdi:identifier", None, True),
    ("messages", "Known messages", None, "mdi:message-text-outline", _M, True),
    # Enhanced timing diagnostics, disabled by default
    ("minarbitrationmicros", "Arbitration min", "µs", "mdi:timer-outline", _M, False),
    ("maxarbitrationmicros", "Arbitration max", "µs", "mdi:timer-outline", _M, False),
    ("minsymbollatency", "Symbol latency min", None, "mdi:timer-sand", _M, False),
    ("maxsymbollatency", "Symbol latency max", None, "mdi:timer-sand", _M, False),
]


def _precision(desc: FieldDesc) -> int | None:
    """Decimal places from the data type's step size (UCH -> 0, D2C -> 1)."""
    if not desc.numeric or not desc.step:
        return None
    if desc.step >= 1:
        return 0
    text = f"{desc.step:g}"
    return len(text.split(".")[1]) if "." in text else 0


def _classes(desc: FieldDesc):
    """device_class + state_class from the unit (combined without conflicts)."""
    unit = desc.unit
    if unit == "°C":
        return SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT
    if unit in ("kW", "W"):
        return SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT
    if unit == "kWh":
        return SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING
    if unit == "bar":
        return SensorDeviceClass.PRESSURE, SensorStateClass.MEASUREMENT
    if unit == "%":
        return None, SensorStateClass.MEASUREMENT
    if desc.numeric:
        return None, SensorStateClass.MEASUREMENT
    return None, None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EbusdCoordinator = hass.data[DOMAIN][entry.entry_id]
    entry.async_on_unload(
        add_fields_dynamically(
            coordinator,
            async_add_entities,
            lambda d: (
                not is_binary(d)
                and not writable_control(d)
                and coordinator.included(d)
            ),
            lambda d: EbusdSensor(coordinator, d),
            always=is_error_status,  # show the error log even without a fault
        )
    )
    async_add_entities(
        EbusdGlobalSensor(coordinator, *spec)
        for spec in _GLOBAL_SENSORS
        if spec[0] in coordinator.global_data
    )
    async_add_entities([EbusdHostSensor(coordinator)])


class EbusdSensor(EbusdBaseEntity, SensorEntity):
    def __init__(self, coordinator: EbusdCoordinator, desc: FieldDesc) -> None:
        super().__init__(coordinator, desc)
        self._attr_unique_id = f"{DOMAIN}_{desc.uid}"
        self._is_error = is_error_status(desc)
        if self._is_error:
            # Error log: a status, not a measurement -> no device_/state_class.
            # An empty slot shows "ok"; the entity stays visible (see available).
            self._attr_icon = "mdi:alert-circle-check-outline"
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            return
        if desc.unit:
            self._attr_native_unit_of_measurement = desc.unit
        self._attr_device_class, self._attr_state_class = _classes(desc)
        precision = _precision(desc)
        if precision is not None:
            self._attr_suggested_display_precision = precision

    @property
    def available(self) -> bool:
        # Error log always visible while ebusd is reachable -- "empty"
        # means "no error", not "unavailable".
        if self._is_error:
            return self.coordinator.last_update_success
        return super().available

    @property
    def native_value(self):
        value = self._value
        if self._is_error:
            return "ok" if value is None else str(value)
        if value is None:
            return None
        if self._desc.numeric:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        return value  # enum/text value (e.g. "auto", "off")


class EbusdGlobalSensor(CoordinatorEntity[EbusdCoordinator], SensorEntity):
    """Bus/adapter diagnostics (global ebusd section), attached to the bridge."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: EbusdCoordinator,
        key: str,
        name: str,
        unit: str | None,
        icon: str,
        state_class: SensorStateClass | None,
        enabled: bool,
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_state_class = state_class
        self._attr_entity_registry_enabled_default = enabled
        self._attr_unique_id = f"{DOMAIN}_{coordinator.entry_id}_global_{key}"
        self._attr_device_info = DeviceInfo(identifiers={coordinator.bridge_id})

    @property
    def native_value(self):
        return self.coordinator.global_data.get(self._key)


class EbusdHostSensor(CoordinatorEntity[EbusdCoordinator], SensorEntity):
    """Host/IP of the ebusd instance as a text sensor on the bridge."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Host"
    _attr_icon = "mdi:ip-network"

    def __init__(self, coordinator: EbusdCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{coordinator.entry_id}_host"
        self._attr_device_info = DeviceInfo(identifiers={coordinator.bridge_id})

    @property
    def native_value(self) -> str:
        return self.coordinator.host
