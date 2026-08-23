"""Base entity and dynamic-device helper shared by every Daikin One platform."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import DeviceSummary, Thermostat
from .const import DOMAIN, MANUFACTURER
from .coordinator import DaikinOneCoordinator

if TYPE_CHECKING:
    from homeassistant.helpers.entity import Entity, EntityDescription
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

__all__ = ["DaikinOneEntity", "async_setup_platform_entities"]


def _device_name(coordinator: DaikinOneCoordinator, summary: DeviceSummary) -> str:
    """Prefix the thermostat name with its location when the account has several."""
    locations = {t.summary.location_name for t in coordinator.data.values() if t.summary.location_name}
    if len(locations) > 1 and summary.location_name:
        return f"{summary.location_name} {summary.name}"
    return summary.name


class DaikinOneEntity(CoordinatorEntity[DaikinOneCoordinator]):
    """An entity backed by one thermostat of the account."""

    _attr_has_entity_name = True
    #: Set to False for entities that stay meaningful while the thermostat is unreachable.
    _requires_online = True

    def __init__(
        self,
        coordinator: DaikinOneCoordinator,
        thermostat_id: str,
        description: EntityDescription | None = None,
    ) -> None:
        """Attach the entity to a thermostat, optionally via an entity description."""
        super().__init__(coordinator)
        self._thermostat_id = thermostat_id
        if description is not None:
            self.entity_description = description
            self._attr_unique_id = f"{thermostat_id}-{description.key}"

        self._last_known = coordinator.data[thermostat_id]
        summary = self._last_known.summary
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, thermostat_id)},
            name=_device_name(coordinator, summary),
            manufacturer=MANUFACTURER,
            model=summary.model,
            sw_version=summary.firmware_version,
        )

    @property
    def thermostat(self) -> Thermostat:
        """The thermostat this entity belongs to, or its last known snapshot.

        A thermostat can drop out of a poll while its entities are still alive (HA reads
        capability attributes even from an unavailable entity), so this never raises: the
        entity reports `unavailable` and keeps its last known capabilities.
        """
        if (thermostat := self.coordinator.data.get(self._thermostat_id)) is not None:
            self._last_known = thermostat
        return self._last_known

    @property
    def available(self) -> bool:
        """Available while the coordinator succeeds and the thermostat is reachable."""
        thermostat = self.coordinator.data.get(self._thermostat_id)
        if thermostat is None:
            return False
        return super().available and (not self._requires_online or thermostat.online)


@callback
def async_setup_platform_entities(
    coordinator: DaikinOneCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[str], list[Entity]],
) -> CALLBACK_TYPE:
    """Add entities for the current thermostats and for any that appear later.

    Returns the coordinator listener's unsubscribe callable; platforms hand it to
    `entry.async_on_unload`.
    """
    known: set[str] = set()

    @callback
    def _async_add_new() -> None:
        if gone := known.difference(coordinator.data):
            # A thermostat can leave the account and come back. Forget it only once the
            # coordinator has really removed its device — and with it its entities — so the
            # return builds fresh entities instead of colliding with surviving ones.
            registry = dr.async_get(coordinator.hass)
            known.difference_update(
                thermostat_id
                for thermostat_id in gone
                if registry.async_get_device(identifiers={(DOMAIN, thermostat_id)}) is None
            )
        new_ids = [thermostat_id for thermostat_id in coordinator.data if thermostat_id not in known]
        if not new_ids:
            return
        known.update(new_ids)
        entities: list[Entity] = []
        for thermostat_id in new_ids:
            entities.extend(factory(thermostat_id))
        async_add_entities(entities)

    _async_add_new()
    return coordinator.async_add_listener(_async_add_new)
