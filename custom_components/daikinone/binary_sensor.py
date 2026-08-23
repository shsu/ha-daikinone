"""Binary sensor platform: reachability, geofencing and what the equipment is running."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity import Entity

from .api import EquipmentStatus, Thermostat
from .entity import DaikinOneEntity, async_setup_platform_entities

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import DaikinOneConfigEntry
    from .coordinator import DaikinOneCoordinator

# Read-only entities driven entirely by the coordinator.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class DaikinOneBinarySensorDescription(BinarySensorEntityDescription):
    """A binary sensor plus how to read it from a thermostat."""

    is_on_fn: Callable[[Thermostat], bool | None]
    #: The `online` sensor must keep reporting while the thermostat is unreachable.
    requires_online: bool = True


def _running(status: EquipmentStatus) -> Callable[[Thermostat], bool | None]:
    """Build a reader that reports whether the equipment is in the given state."""

    def _is_on(thermostat: Thermostat) -> bool | None:
        current = thermostat.state.equipment_status
        if current is None or current is EquipmentStatus.UNKNOWN:
            return None
        return current is status

    return _is_on


BINARY_SENSORS: Final[tuple[DaikinOneBinarySensorDescription, ...]] = (
    DaikinOneBinarySensorDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda thermostat: thermostat.online,
        requires_online=False,
    ),
    DaikinOneBinarySensorDescription(
        key="geofencing",
        translation_key="geofencing",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda thermostat: thermostat.state.geofencing_enabled,
    ),
    DaikinOneBinarySensorDescription(
        key="heating",
        translation_key="heating",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        is_on_fn=_running(EquipmentStatus.HEAT),
    ),
    DaikinOneBinarySensorDescription(
        key="cooling",
        translation_key="cooling",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        is_on_fn=_running(EquipmentStatus.COOL),
    ),
    DaikinOneBinarySensorDescription(
        key="dehumidifying",
        translation_key="dehumidifying",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        is_on_fn=_running(EquipmentStatus.OVERCOOL_DEHUM),
    ),
    DaikinOneBinarySensorDescription(
        key="fan_running",
        translation_key="fan_running",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        is_on_fn=_running(EquipmentStatus.FAN),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DaikinOneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors of every thermostat, now and as new ones appear."""
    coordinator = entry.runtime_data

    def _factory(thermostat_id: str) -> list[Entity]:
        return [DaikinOneBinarySensor(coordinator, thermostat_id, description) for description in BINARY_SENSORS]

    entry.async_on_unload(async_setup_platform_entities(coordinator, async_add_entities, _factory))


class DaikinOneBinarySensor(DaikinOneEntity, BinarySensorEntity):
    """One boolean fact about a thermostat."""

    entity_description: DaikinOneBinarySensorDescription

    def __init__(
        self,
        coordinator: DaikinOneCoordinator,
        thermostat_id: str,
        description: DaikinOneBinarySensorDescription,
    ) -> None:
        """Attach the sensor and honour its availability rule."""
        super().__init__(coordinator, thermostat_id, description)
        self._requires_online = description.requires_online

    @property
    def is_on(self) -> bool | None:
        """The fact, or None while the thermostat has not reported it."""
        return self.entity_description.is_on_fn(self.thermostat)
