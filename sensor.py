"""Sensor platform for ABL eMS Home."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import ABLEMSHomeCoordinator, ABLEMSHomeData


@dataclass
class ABLSensorEntityDescription(SensorEntityDescription):
    """Extends SensorEntityDescription with a value extractor."""
    value_fn: Callable[[ABLEMSHomeData], float | int | str | None] = lambda _: None


SENSOR_DESCRIPTIONS: tuple[ABLSensorEntityDescription, ...] = (
    # ── e-mobility state ─────────────────────────────────────────────────────
    ABLSensorEntityDescription(
        key="ev_charging_power_total",
        name="EV Charging Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:ev-station",
        value_fn=lambda d: round(d.emobility_state.ev_charging_power.total / 1_000_000, 3),
    ),
    ABLSensorEntityDescription(
        key="ev_charging_power_l1",
        name="EV Charging Power L1",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lightning-bolt",
        entity_registry_enabled_default=False,
        value_fn=lambda d: round(d.emobility_state.ev_charging_power.l1 / 1_000_000, 3),
    ),
    ABLSensorEntityDescription(
        key="ev_charging_power_l2",
        name="EV Charging Power L2",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lightning-bolt",
        entity_registry_enabled_default=False,
        value_fn=lambda d: round(d.emobility_state.ev_charging_power.l2 / 1_000_000, 3),
    ),
    ABLSensorEntityDescription(
        key="ev_charging_power_l3",
        name="EV Charging Power L3",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lightning-bolt",
        entity_registry_enabled_default=False,
        value_fn=lambda d: round(d.emobility_state.ev_charging_power.l3 / 1_000_000, 3),
    ),
    ABLSensorEntityDescription(
        key="curtailment_setpoint",
        name="Curtailment Setpoint",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:transmission-tower-off",
        entity_registry_enabled_default=False,
        value_fn=lambda d: round(d.emobility_state.curtailment_setpoint.total / 1_000_000, 3),
    ),
    # ── smart meter (WebSocket, real-time) ───────────────────────────────────
    # SmartMeterReading stores power in W (raw mW already divided by 1000).
    # Sensors display in kW so divide by 1000 here.
    # All values are GRID totals (EV + house load combined).
    ABLSensorEntityDescription(
        key="grid_power_total",
        name="Grid Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:transmission-tower",
        value_fn=lambda d: round(d.smart_meter.power_total / 1000, 3) if d.smart_meter else None,
    ),
    ABLSensorEntityDescription(
        key="grid_power_l1",
        name="Grid Power L1",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lightning-bolt",
        entity_registry_enabled_default=False,
        value_fn=lambda d: round(d.smart_meter.power_l1 / 1000, 3) if d.smart_meter else None,
    ),
    ABLSensorEntityDescription(
        key="grid_power_l2",
        name="Grid Power L2",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lightning-bolt",
        entity_registry_enabled_default=False,
        value_fn=lambda d: round(d.smart_meter.power_l2 / 1000, 3) if d.smart_meter else None,
    ),
    ABLSensorEntityDescription(
        key="grid_power_l3",
        name="Grid Power L3",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lightning-bolt",
        entity_registry_enabled_default=False,
        value_fn=lambda d: round(d.smart_meter.power_l3 / 1000, 3) if d.smart_meter else None,
    ),
    ABLSensorEntityDescription(
        key="grid_apparent_power_total",
        name="Grid Apparent Power",
        native_unit_of_measurement="kVA",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:transmission-tower",
        entity_registry_enabled_default=False,
        value_fn=lambda d: round(d.smart_meter.power_apparent / 1000, 3) if d.smart_meter else None,
    ),
    ABLSensorEntityDescription(
        key="grid_voltage_l1",
        name="Grid Voltage L1",
        native_unit_of_measurement="V",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: round(d.smart_meter.voltage_l1, 2) if d.smart_meter else None,
    ),
    ABLSensorEntityDescription(
        key="grid_voltage_l2",
        name="Grid Voltage L2",
        native_unit_of_measurement="V",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: round(d.smart_meter.voltage_l2, 2) if d.smart_meter else None,
    ),
    ABLSensorEntityDescription(
        key="grid_voltage_l3",
        name="Grid Voltage L3",
        native_unit_of_measurement="V",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: round(d.smart_meter.voltage_l3, 2) if d.smart_meter else None,
    ),
    ABLSensorEntityDescription(
        key="grid_current_l1",
        name="Grid Current L1",
        native_unit_of_measurement="A",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: round(d.smart_meter.current_l1, 3) if d.smart_meter else None,
    ),
    ABLSensorEntityDescription(
        key="grid_current_l2",
        name="Grid Current L2",
        native_unit_of_measurement="A",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: round(d.smart_meter.current_l2, 3) if d.smart_meter else None,
    ),
    ABLSensorEntityDescription(
        key="grid_current_l3",
        name="Grid Current L3",
        native_unit_of_measurement="A",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: round(d.smart_meter.current_l3, 3) if d.smart_meter else None,
    ),
    ABLSensorEntityDescription(
        key="grid_frequency",
        name="Grid Frequency",
        native_unit_of_measurement="Hz",
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: round(d.smart_meter.frequency, 3) if d.smart_meter else None,
    ),
    ABLSensorEntityDescription(
        key="grid_energy_import_total",
        name="Grid Energy Import",
        native_unit_of_measurement="kWh",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:meter-electric",
        value_fn=lambda d: round(d.smart_meter.energy_total, 3) if d.smart_meter else None,
    ),
    # ── EV charging state ────────────────────────────────────────────────────
    ABLSensorEntityDescription(
        key="ev_charging_state",
        name="EV Charging State",
        icon="mdi:ev-station",
        value_fn=lambda d: (
            "locked"   if d.charge_mode.mode == "lock" else
            "charging" if d.emobility_state.ev_charging_power.total > 0 else
            "idle"
        ),
    ),
    # ── charge mode ──────────────────────────────────────────────────────────
    ABLSensorEntityDescription(
        key="charge_mode",
        name="Charge Mode",
        icon="mdi:car-electric",
        value_fn=lambda d: d.charge_mode.mode,
    ),
    ABLSensorEntityDescription(
        key="min_pv_power_quota",
        name="Min PV Power Quota",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:solar-power",
        value_fn=lambda d: d.charge_mode.min_pv_power_quota,
    ),
    # ── device health ─────────────────────────────────────────────────────────
    ABLSensorEntityDescription(
        key="device_status",
        name="Device Status",
        icon="mdi:information-outline",
        value_fn=lambda d: d.device_status.status,
    ),
    ABLSensorEntityDescription(
        key="cpu_load",
        name="CPU Load",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:cpu-64-bit",
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.device_status.cpu_load,
    ),
    ABLSensorEntityDescription(
        key="cpu_temp",
        name="CPU Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.device_status.cpu_temp,
    ),
    ABLSensorEntityDescription(
        key="ram_used_pct",
        name="RAM Usage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:memory",
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.device_status.ram_used_pct,
    ),
    ABLSensorEntityDescription(
        key="flash_data_used_pct",
        name="Flash Data Usage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:harddisk",
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.device_status.flash_data_used_pct,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ABL eMS Home sensors."""
    coordinator: ABLEMSHomeCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]

    async_add_entities(
        ABLEMSHomeSensor(coordinator, entry, description)
        for description in SENSOR_DESCRIPTIONS
    )


class ABLEMSHomeSensor(CoordinatorEntity[ABLEMSHomeCoordinator], SensorEntity):
    """A single sensor entity backed by the coordinator."""

    entity_description: ABLSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ABLEMSHomeCoordinator,
        entry: ConfigEntry,
        description: ABLSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="ABL eMS Home",
            manufacturer="ABL",
            model="eMS Home",
            configuration_url=f"http://{entry.data['host']}:{entry.data.get('port', 80)}",
            model_id="ems-home",
        )

    @property
    def native_value(self) -> float | int | str | None:
        if self.coordinator.data is None:
            return None
        try:
            return self.entity_description.value_fn(self.coordinator.data)
        except Exception:
            return None
