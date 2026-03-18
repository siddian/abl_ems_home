"""DataUpdateCoordinator for ABL eMS Home."""
from __future__ import annotations

import logging
from datetime import timedelta
from dataclasses import dataclass
from typing import Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .abl_ems_home import EMSHomeHTTP, DeviceStatus, EMobilityState, ChargeModeConfig
from .smart_meter_ws import SmartMeterReading, SmartMeterWebSocket
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass
class ABLEMSHomeData:
    """All data available to sensor / select entities."""
    device_status:   DeviceStatus
    emobility_state: EMobilityState
    charge_mode:     ChargeModeConfig
    smart_meter: Optional[SmartMeterReading] = None


class ABLEMSHomeCoordinator(DataUpdateCoordinator[ABLEMSHomeData]):
    """
    Polls the eMS Home HTTP endpoints on a fixed interval and incorporates
    smart meter data pushed via WebSocket in real time.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: EMSHomeHTTP,
        update_interval: int,
    ) -> None:
        self.client = client
        self._ws_client: Optional[SmartMeterWebSocket] = None
        self._latest_smart_meter: Optional[SmartMeterReading] = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=update_interval),
        )

    # ------------------------------------------------------------------
    # WebSocket lifecycle
    # ------------------------------------------------------------------

    async def async_start_websocket(self) -> None:
        """Start the smart meter WebSocket listener."""
        token = self.client.token
        if token is None:
            _LOGGER.warning("Cannot start smart meter WS – no token available")
            return

        # Extract host and port from the client base URL
        base = self.client._base          # e.g. "http://ems-home-12345678:80"
        host = base.split("//")[1].rsplit(":", 1)[0]
        try:
            port = int(base.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            port = 80

        self._ws_client = SmartMeterWebSocket(
            host=host,
            token=token,
            on_reading=self._on_smart_meter_reading,
            port=port,
        )
        await self._ws_client.start()
        _LOGGER.debug("Smart meter WebSocket started for %s:%s", host, port)

    async def async_stop_websocket(self) -> None:
        """Stop the smart meter WebSocket listener."""
        if self._ws_client:
            await self._ws_client.stop()
            self._ws_client = None

    @callback
    def _on_smart_meter_reading(self, reading: SmartMeterReading) -> None:
        """Called from the WS task whenever a new frame arrives."""
        self._latest_smart_meter = reading
        if self.data is not None:
            self.data.smart_meter = reading
            self.async_set_updated_data(self.data)

    # ------------------------------------------------------------------
    # HTTP poll
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> ABLEMSHomeData:
        """Fetch HTTP data on the configured poll interval."""
        try:
            device_status, emobility_state, charge_mode = (
                await self.hass.async_add_executor_job(self._fetch_all)
            )
        except Exception as exc:
            raise UpdateFailed(f"Error communicating with eMS Home: {exc}") from exc

        return ABLEMSHomeData(
            device_status=device_status,
            emobility_state=emobility_state,
            charge_mode=charge_mode,
            smart_meter=self._latest_smart_meter,
        )

    def _fetch_all(self):
        """Blocking HTTP calls – runs in the executor thread pool."""
        # Keep WS token in sync whenever HTTP auto-renews it
        if self._ws_client and self.client.token:
            self._ws_client.update_token(self.client.token)

        device_status   = self.client.get_device_status()
        emobility_state = self.client.get_emobility_state()
        charge_mode     = self.client.get_charge_mode()
        return device_status, emobility_state, charge_mode
