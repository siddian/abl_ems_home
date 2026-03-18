"""
ABL eMS Home Python Interface
==============================
A Python library for communicating with the ABL eMS Home energy management
system and its connected eMH1 wallboxes.

Two communication layers are supported:

1. **HTTP layer** - the eMS Home exposes a local web interface (accessible
   by IP address on your LAN).  Use ``EMSHomeHTTP`` to authenticate, read
   the dashboard / system data and change configuration through the web UI.

2. **Modbus ASCII layer** - the eMH1 wallboxes speak Modbus ASCII over
   RS485 (38400 Bd, 8E1).  Use ``EMH1ModbusASCII`` to talk directly to
   individual wallboxes.  You can reach the RS485 bus either through:
   - a local serial port / USB-RS485 adapter  (``serial://`` transport)
   - a transparent RS485-to-TCP gateway (e.g. Protoss PW11-H, USR-TCP232)

Dependencies
------------
    pip install requests pyserial

Usage example
-------------
    # --- HTTP interface (OAuth2 Bearer token, auto-managed) ---
    from abl_ems_home import EMSHomeHTTP

    # host can be IP or hostname; password is on the unit's rating plate
    with EMSHomeHTTP("ems-home-12345678", password="yourpassword") as ems:
        # Discover real endpoint paths on your firmware version:
        print(ems.explore("/api/"))
        print(ems.explore("/api/devices"))
        # Once confirmed, use the typed helpers:
        print(ems.get_system_info())

    # --- Modbus ASCII over serial ---
    from abl_ems_home import EMH1ModbusASCII

    wb = EMH1ModbusASCII.from_serial("/dev/ttyUSB0")
    print(wb.read_firmware(device_id=1))
    wb.set_max_current(device_id=1, amps=10)
    wb.close()

    # --- Modbus ASCII over TCP gateway ---
    wb = EMH1ModbusASCII.from_tcp("192.168.1.50", port=8899)
    print(wb.read_current(device_id=1))
    wb.close()
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

# ---------------------------------------------------------------------------
# Optional imports - graceful degradation when packages are not installed
# ---------------------------------------------------------------------------
try:
    import requests
    from requests import Session

    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

try:
    import serial as _serial

    _HAS_SERIAL = True
except ImportError:
    _HAS_SERIAL = False


# ===========================================================================
# Data classes / enumerations
# ===========================================================================


class ChargeState(IntEnum):
    """IEC 61851 / SAE J1772 charging states reported by the EVCC."""

    A1 = 0xA1  # No EV connected, outlet enabled
    A2 = 0xA2  # No EV connected, outlet disabled
    B1 = 0xB1  # EV connected, no charging request
    B2 = 0xB2  # EV connected, ready to charge
    C1 = 0xC1  # Charging (I = 0)
    C2 = 0xC2  # Charging (I > 1 A)
    E0 = 0xE0  # Error / disabled by EMS
    E2 = 0xE2  # Error state E2
    F1 = 0xF1  # RCD tripped
    UNKNOWN = 0x00


@dataclass
class FirmwareInfo:
    device_id: int
    hardware_revision: str
    firmware_major: int
    firmware_minor: int
    socket_enabled: bool
    welding_detection: bool
    phase_meter: bool
    rdc_md: bool
    upstream_timeout: bool


@dataclass
class CurrentReading:
    """Per-phase current readings from an eMH1 wallbox."""

    device_id: int
    state: ChargeState
    max_current_amps: int  # Icmax currently set
    phase1_amps: int
    phase2_amps: int
    phase3_amps: int

    @property
    def total_power_kw(self) -> float:
        """Approximate 3-phase power in kW (assumes 230 V per phase)."""
        return round(
            (self.phase1_amps + self.phase2_amps + self.phase3_amps) * 230 / 1000, 2
        )


@dataclass
class DeviceStatus:
    """System health snapshot from ``/api/device-settings/devicestatus``."""

    status: str  # e.g. "idle", "charging"
    cpu_load: int  # percent
    cpu_temp: int  # °C
    ram_free: int  # bytes
    ram_total: int  # bytes
    flash_app_free: int  # bytes
    flash_app_total: int  # bytes
    flash_data_free: int  # bytes
    flash_data_total: int  # bytes

    @property
    def ram_used_pct(self) -> float:
        """RAM utilisation as a percentage."""
        return round((1 - self.ram_free / self.ram_total) * 100, 1)

    @property
    def flash_app_used_pct(self) -> float:
        """App-partition flash utilisation as a percentage."""
        return round((1 - self.flash_app_free / self.flash_app_total) * 100, 1)

    @property
    def flash_data_used_pct(self) -> float:
        """Data-partition flash utilisation as a percentage."""
        return round((1 - self.flash_data_free / self.flash_data_total) * 100, 1)

    @classmethod
    def from_dict(cls, d: dict) -> "DeviceStatus":
        return cls(
            status=d.get("status", "unknown"),
            cpu_load=d.get("CpuLoad", 0),
            cpu_temp=d.get("CpuTemp", 0),
            ram_free=d.get("RamFree", 0),
            ram_total=d.get("RamTotal", 1),
            flash_app_free=d.get("FlashAppFree", 0),
            flash_app_total=d.get("FlashAppTotal", 1),
            flash_data_free=d.get("FlashDataFree", 0),
            flash_data_total=d.get("FlashDataTotal", 1),
        )


@dataclass
class SystemFlags:
    device_id: int
    state_machine_pointer: int
    raw_flags: int


@dataclass
class PhaseValues:
    """A measurement broken down across three phases plus a total."""

    total: float
    l1: float
    l2: float
    l3: float

    @classmethod
    def from_dict(cls, d: dict) -> "PhaseValues":
        return cls(
            total=d.get("total", 0),
            l1=d.get("l1", 0),
            l2=d.get("l2", 0),
            l3=d.get("l3", 0),
        )


@dataclass
class EMobilityState:
    """
    Live e-mobility charging state from ``/api/e-mobility/state``.

    Fields
    ------
    ev_charging_power : PhaseValues
        Active charging power in W per phase and total.
    curtailment_setpoint : PhaseValues
        Current curtailment setpoint applied by load management (W).
    overload_protection_active : bool
        True when the overload protection limiter is actively curtailing.
    """

    ev_charging_power: PhaseValues
    curtailment_setpoint: PhaseValues
    overload_protection_active: bool

    @property
    def is_charging(self) -> bool:
        """True when the total charging power is greater than zero."""
        return self.ev_charging_power.total > 0

    @property
    def total_power_w(self) -> float:
        """Total EV charging power in W (raw values are milliwatts)."""
        return round(self.ev_charging_power.total / 1000, 1)

    @property
    def total_power_kw(self) -> float:
        """Total EV charging power in kW."""
        return round(self.ev_charging_power.total / 1_000_000, 3)

    @classmethod
    def from_dict(cls, d: dict) -> "EMobilityState":
        return cls(
            ev_charging_power=PhaseValues.from_dict(d.get("EvChargingPower", {})),
            curtailment_setpoint=PhaseValues.from_dict(
                d.get("CurtailmentSetpoint", {})
            ),
            overload_protection_active=d.get("OverloadProtectionActive", False),
        )


class ChargeMode(str):
    """
    Known charge-mode strings used by the eMS Home firmware.
    Use as plain strings or compare against these constants.
    """

    GRID = "grid"  # Charge at full grid power          ✓ confirmed
    LOCK = "lock"  # No charging                        ✓ confirmed
    PV = "pv"  # PV-surplus only charging           ✓ confirmed
    HYBRID = "hybrid"  # PV-surplus + grid top-up           ✓ confirmed
    MIN = "min"  # Minimum charging power (unconfirmed)


@dataclass
class ChargeModeConfig:
    """
    Charge-mode configuration from ``/api/e-mobility/config/chargemode``.

    Fields
    ------
    mode : str
        Active mode — one of ``"grid"``, ``"lock"``, ``"pv"``, etc.
    min_charging_power_quota : int
        Minimum charging power quota in % (0 = not set).
    min_pv_power_quota : int
        Minimum PV surplus quota required before charging starts (%).
    last_min_charging_power_quota : int
        Previous value of ``min_charging_power_quota`` (retained by firmware).
    last_min_pv_power_quota : int
        Previous value of ``min_pv_power_quota`` (retained by firmware).
    """

    mode: str
    min_charging_power_quota: int
    min_pv_power_quota: int
    last_min_charging_power_quota: int
    last_min_pv_power_quota: int

    @property
    def is_locked(self) -> bool:
        return self.mode == ChargeMode.LOCK

    @property
    def is_grid_charging(self) -> bool:
        return self.mode == ChargeMode.GRID

    @property
    def is_pv_charging(self) -> bool:
        return self.mode == ChargeMode.PV

    @property
    def is_hybrid_charging(self) -> bool:
        return self.mode == ChargeMode.HYBRID

    @classmethod
    def from_dict(cls, d: dict) -> "ChargeModeConfig":
        return cls(
            mode=d.get("mode", "unknown"),
            min_charging_power_quota=d.get("mincharginpowerquota") or 0,
            min_pv_power_quota=d.get("minpvpowerquota") or 0,
            last_min_charging_power_quota=d.get("lastminchargingpowerquota") or 0,
            last_min_pv_power_quota=d.get("lastminpvpowerquota") or 0,
        )

    def to_payload(self) -> dict:
        """Serialise back to the wire format expected by the PUT endpoint."""
        return {
            "mode": self.mode,
            "mincharginpowerquota": self.min_charging_power_quota or None,
            "minpvpowerquota": self.min_pv_power_quota,
        }


@dataclass
class EVParameters:
    """
    Parameters for a single EV reported by ``/api/e-mobility/evparameterlist``.

    Each connected EV is keyed by a UUID assigned by the eMS Home firmware.
    """

    uuid: str
    min_current: float  # A
    max_current: float  # A
    phases_total: bool
    phase_l1: bool
    phase_l2: bool
    phase_l3: bool
    probing_successful: bool

    @property
    def active_phases(self) -> list[str]:
        """List of active phase labels, e.g. ['L1', 'L3']."""
        return [
            p
            for p, active in [
                ("L1", self.phase_l1),
                ("L2", self.phase_l2),
                ("L3", self.phase_l3),
            ]
            if active
        ]

    @classmethod
    def from_dict(cls, uuid: str, d: dict) -> "EVParameters":
        phases = d.get("phases_used", {})
        return cls(
            uuid=uuid,
            min_current=d.get("min_current", 0),
            max_current=d.get("max_current", 0),
            phases_total=phases.get("total", False),
            phase_l1=phases.get("l1", False),
            phase_l2=phases.get("l2", False),
            phase_l3=phases.get("l3", False),
            probing_successful=d.get("probing_successful", False),
        )


# ===========================================================================
# Modbus ASCII helpers
# ===========================================================================


def _lrc(data: bytes) -> int:
    """Compute Modbus ASCII LRC (two's complement of sum of bytes)."""
    return -(sum(data)) & 0xFF


def _build_read_frame(device_id: int, start_reg: int, qty: int) -> bytes:
    """Build a Modbus ASCII read-frame string (without leading ':' / CRLF)."""
    payload = bytes(
        [
            device_id,
            0x03,
            (start_reg >> 8) & 0xFF,
            start_reg & 0xFF,
            (qty >> 8) & 0xFF,
            qty & 0xFF,
        ]
    )
    lrc = _lrc(payload)
    hex_body = payload.hex().upper() + f"{lrc:02X}"
    return f":{hex_body}\r\n".encode("ascii")


def _build_write_frame(device_id: int, start_reg: int, values: list[int]) -> bytes:
    """Build a Modbus ASCII write-frame (function 0x10)."""
    qty = len(values)
    byte_count = qty * 2
    header = bytes(
        [
            device_id,
            0x10,
            (start_reg >> 8) & 0xFF,
            start_reg & 0xFF,
            (qty >> 8) & 0xFF,
            qty & 0xFF,
            byte_count,
        ]
    )
    value_bytes = b""
    for v in values:
        value_bytes += struct.pack(">H", v)
    payload = header + value_bytes
    lrc = _lrc(payload)
    hex_body = payload.hex().upper() + f"{lrc:02X}"
    return f":{hex_body}\r\n".encode("ascii")


def _parse_response(raw: bytes) -> list[int]:
    """
    Parse a Modbus ASCII response frame (starting with '>').
    Returns a list of 16-bit register values.
    Raises ValueError on malformed / error frames.
    """
    text = raw.decode("ascii", errors="replace").strip()

    if not text:
        raise ValueError("Empty response from device")

    # Strip leading '>' if present
    if text.startswith(">"):
        text = text[1:]

    # Error frame: function code has bit 7 set (e.g. 0x90)
    raw_bytes = bytes.fromhex(text[:-2])  # exclude LRC hex chars
    if len(raw_bytes) < 2:
        raise ValueError(f"Response too short: {text!r}")

    func_code = raw_bytes[1]
    if func_code & 0x80:
        exception = raw_bytes[2] if len(raw_bytes) > 2 else 0
        raise ValueError(f"Modbus error response - exception code 0x{exception:02X}")

    # Verify LRC
    body = bytes.fromhex(text)
    if _lrc(body[:-1]) != body[-1]:
        raise ValueError(f"LRC mismatch in response: {text!r}")

    # Write-response: device_id + 0x10 + addr(2) + qty(2) + lrc = 6 bytes
    if func_code == 0x10:
        return []

    # Read-response: device_id + 0x03 + byte_count + data... + lrc
    byte_count = raw_bytes[2]
    data = raw_bytes[3 : 3 + byte_count]
    registers: list[int] = []
    for i in range(0, len(data), 2):
        registers.append(struct.unpack(">H", data[i : i + 2])[0])
    return registers


# ===========================================================================
# Transport abstraction
# ===========================================================================


class _SerialTransport:
    """Send/receive over a real RS485 serial port."""

    def __init__(self, port: str, baudrate: int = 38400, timeout: float = 1.0):
        if not _HAS_SERIAL:
            raise ImportError(
                "pyserial is required for serial transport: pip install pyserial"
            )
        self._ser = _serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=_serial.EIGHTBITS,
            parity=_serial.PARITY_EVEN,
            stopbits=_serial.STOPBITS_ONE,
            timeout=timeout,
        )

    def send_recv(self, frame: bytes, retries: int = 3) -> bytes:
        for attempt in range(retries):
            try:
                self._ser.reset_input_buffer()
                self._ser.write(frame)
                # Read until CRLF
                response = b""
                deadline = time.time() + self._ser.timeout
                while time.time() < deadline:
                    chunk = self._ser.read(64)
                    if chunk:
                        response += chunk
                    if b"\r\n" in response:
                        break
                if response:
                    return response.strip()
            except Exception:
                if attempt == retries - 1:
                    raise
                time.sleep(0.1 * (attempt + 1))
        raise TimeoutError("No response from wallbox after retries")

    def close(self):
        if self._ser.is_open:
            self._ser.close()


class _TCPTransport:
    """Send/receive over a transparent RS485-to-TCP gateway."""

    def __init__(self, host: str, port: int, timeout: float = 3.0):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None

    def _connect(self):
        if self._sock is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self._timeout)
            s.connect((self._host, self._port))
            self._sock = s

    def send_recv(self, frame: bytes, retries: int = 3) -> bytes:
        for attempt in range(retries):
            try:
                self._connect()
                assert self._sock is not None
                self._sock.sendall(frame)
                response = b""
                deadline = time.time() + self._timeout
                while time.time() < deadline:
                    try:
                        chunk = self._sock.recv(256)
                        if chunk:
                            response += chunk
                        if b"\r\n" in response:
                            break
                    except socket.timeout:
                        break
                if response:
                    return response.strip()
            except (OSError, BrokenPipeError):
                # Reconnect on next retry
                self._sock = None
                if attempt == retries - 1:
                    raise
                time.sleep(0.2 * (attempt + 1))
        raise TimeoutError("No response from wallbox gateway after retries")

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None


# ===========================================================================
# EMH1 Modbus ASCII client
# ===========================================================================


class EMH1ModbusASCII:
    """
    Communicate directly with ABL eMH1 wallboxes via Modbus ASCII (RS485).

    The eMH1 uses a *non-standard* Modbus ASCII variant: response frames
    start with '>' instead of the standard ':'.  This class handles that.

    Parameters
    ----------
    transport:
        Either a ``_SerialTransport`` or ``_TCPTransport`` instance.
        Use the convenience class-methods ``from_serial`` and ``from_tcp``.
    inter_frame_delay:
        Minimum delay (seconds) between consecutive requests.  The wallbox
        sometimes needs to be 'woken up' - a small delay helps reliability.
    """

    # ABL-specific: responses start with '>' not ':'
    RESPONSE_MARKER = b">"

    def __init__(self, transport, inter_frame_delay: float = 0.05):
        self._transport = transport
        self._delay = inter_frame_delay

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def from_serial(
        cls, port: str, baudrate: int = 38400, timeout: float = 1.0, **kwargs
    ) -> "EMH1ModbusASCII":
        """Create an instance using a local serial / USB-RS485 adapter."""
        transport = _SerialTransport(port, baudrate=baudrate, timeout=timeout)
        return cls(transport, **kwargs)

    @classmethod
    def from_tcp(
        cls, host: str, port: int = 8899, timeout: float = 3.0, **kwargs
    ) -> "EMH1ModbusASCII":
        """Create an instance using a transparent RS485-over-TCP gateway."""
        transport = _TCPTransport(host, port=port, timeout=timeout)
        return cls(transport, **kwargs)

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _read_registers(self, device_id: int, start: int, qty: int) -> list[int]:
        frame = _build_read_frame(device_id, start, qty)
        time.sleep(self._delay)
        raw = self._transport.send_recv(frame)
        return _parse_response(raw)

    def _write_registers(self, device_id: int, start: int, values: list[int]) -> None:
        frame = _build_write_frame(device_id, start, values)
        time.sleep(self._delay)
        raw = self._transport.send_recv(frame)
        _parse_response(raw)  # raises on error

    # ------------------------------------------------------------------
    # Public API  (mirrors the official Modbus register map)
    # ------------------------------------------------------------------

    def read_firmware(self, device_id: int = 1) -> FirmwareInfo:
        """
        Register 0x0001-0x0002: Read device ID and firmware revision.
        Broadcast (device_id=0) is supported.
        """
        regs = self._read_registers(device_id, 0x0001, 2)
        r1, r2 = regs[0], regs[1]

        hw_map = {0: "pcba:141215", 1: "pcba:160307", 2: "pcba:170725"}
        hw_rev = hw_map.get((r1 >> 6) & 0x03, "unknown")
        dev_id_reported = (r1 >> 0) & 0x1F
        firmware_major = (r2 >> 12) & 0x0F
        firmware_minor = (r2 >> 8) & 0x0F
        flags = r2 & 0xFF

        return FirmwareInfo(
            device_id=dev_id_reported,
            hardware_revision=hw_rev,
            firmware_major=firmware_major,
            firmware_minor=firmware_minor,
            socket_enabled=bool(flags & 0x08),
            welding_detection=bool(flags & 0x04),
            phase_meter=bool(flags & 0x20),
            rdc_md=bool(flags & 0x10),
            upstream_timeout=bool(flags & 0x40),
        )

    def read_modbus_settings(self, device_id: int = 1) -> dict:
        """
        Register 0x0003: Read current Modbus serial settings.
        Returns a dict with keys: baudrate, parity, stop_bits.
        """
        regs = self._read_registers(device_id, 0x0003, 1)
        r = regs[0] & 0xFF  # lower byte holds settings
        baud_map = {5: 9600, 6: 19200, 7: 38400, 8: 57600}
        parity_map = {0: "none", 1: "odd", 2: "even"}
        stop_map = {0: 1, 3: 2}
        return {
            "baudrate": baud_map.get(r & 0x0F, "unknown"),
            "parity": parity_map.get((r >> 4) & 0x03, "unknown"),
            "stop_bits": stop_map.get((r >> 6) & 0x03, "unknown"),
        }

    def read_current(self, device_id: int = 1) -> CurrentReading:
        """
        Register 0x0033-0x0035 (short form): Read current per phase + state.
        This is the most efficient way to poll charging status.
        """
        regs = self._read_registers(device_id, 0x0033, 3)
        r0, r1, r2 = regs[0], regs[1], regs[2]

        # High byte of r0: UCP / Icmax flags; low byte: state
        state_byte = r0 & 0xFF
        try:
            state = ChargeState(state_byte)
        except ValueError:
            state = ChargeState.UNKNOWN

        # Icmax is encoded in the duty-cycle word; r1 high byte = Icmax amps
        icmax_raw = (r0 >> 8) & 0xFF
        # Decode: Icmax value * 10 / 0.6 ≈ duty cycle%, but amps are stored directly
        # Per spec, 0x0A = 10A, 0x10 = 16A, etc.
        icmax_amps = icmax_raw  # value is already in amps

        phase1 = r1 & 0xFF
        phase2 = (r1 >> 8) & 0xFF
        phase3 = r2 & 0xFF

        return CurrentReading(
            device_id=device_id,
            state=state,
            max_current_amps=icmax_amps,
            phase1_amps=phase1,
            phase2_amps=phase2,
            phase3_amps=phase3,
        )

    def read_current_full(self, device_id: int = 1) -> CurrentReading:
        """
        Register 0x002E-0x0032 (long form): Read full current data block.
        Includes UCP voltage and per-phase ICT values.
        """
        regs = self._read_registers(device_id, 0x002E, 5)
        r0 = regs[0]
        state_byte = r0 & 0xFF
        try:
            state = ChargeState(state_byte)
        except ValueError:
            state = ChargeState.UNKNOWN

        icmax_raw = (regs[1] >> 8) & 0xFF
        phase1 = regs[2] & 0xFF
        phase2 = regs[3] & 0xFF
        phase3 = regs[4] & 0xFF

        return CurrentReading(
            device_id=device_id,
            state=state,
            max_current_amps=icmax_raw,
            phase1_amps=phase1,
            phase2_amps=phase2,
            phase3_amps=phase3,
        )

    def read_system_flags(self, device_id: int = 1) -> SystemFlags:
        """Register 0x0006-0x0007: Read system flags (state machine pointer etc.)."""
        regs = self._read_registers(device_id, 0x0006, 2)
        ptr = (regs[0] >> 8) & 0xFF
        flags = ((regs[0] & 0xFF) << 16) | regs[1]
        return SystemFlags(
            device_id=device_id, state_machine_pointer=ptr, raw_flags=flags
        )

    def set_max_current(self, device_id: int = 1, amps: int = 16) -> None:
        """
        Register 0x0014: Set maximum charge current (Icmax).

        Parameters
        ----------
        device_id : int
            Wallbox device ID (1-16, or 0 for broadcast).
        amps : int
            Desired max current in amps (6-16 in 1 A steps, or 0 to disable).
        """
        if amps not in (0, *range(6, 17)):
            raise ValueError(f"Invalid current {amps}A - must be 0 or 6..16")
        # Duty cycle: amps / 0.6 * 10 ≈ raw value
        # Per official spec table: 10A → 0x00A6, 16A → 0x0109 (duty cycle %)
        # Simpler: the raw word is just amps * 10 for the lower byte approach.
        # Official example: 10A → write 0x00A6 (duty 16.6%)
        # Formula: duty_raw = round(amps / 0.6)
        duty_raw = round(amps / 0.6) if amps > 0 else 0
        self._write_registers(device_id, 0x0014, [duty_raw])

    def modify_state(self, device_id: int = 1, state_cmd: int = 0xE2E2) -> None:
        """
        Register 0x0005: Send a state-modification command.

        Common values:
        - 0xE0E0  → Jump to error/disabled state E0
        - 0xE2E2  → Enable charging (state E2, requires A1 or E0)
        - 0x5A5A  → Reset
        - 0xA1A1  → Jump to A1 (outlet enabled, no EV)
        - 0xF1F1  → Trip RCD
        """
        self._write_registers(device_id, 0x0005, [state_cmd])

    def enable_charging(self, device_id: int = 1) -> None:
        """Enable charging on an outlet (set state E2)."""
        self.modify_state(device_id, 0xE2E2)

    def disable_charging(self, device_id: int = 1) -> None:
        """Disable charging on an outlet (set state E0)."""
        self.modify_state(device_id, 0xE0E0)

    def reset(self, device_id: int = 0) -> None:
        """Broadcast or targeted reset command."""
        self.modify_state(device_id, 0x5A5A)

    def set_device_id(self, current_id: int, new_id: int) -> None:
        """
        Register 0x002C: Reassign a wallbox's Modbus device ID.
        Caution: use this only if you know what you're doing!
        """
        if not 1 <= new_id <= 16:
            raise ValueError("Device ID must be in range 1..16")
        self._write_registers(current_id, 0x002C, [new_id])

    def scan_bus(self, max_id: int = 16, timeout_per_id: float = 0.3) -> list[int]:
        """
        Scan the RS485 bus for connected wallboxes.
        Returns a list of responding device IDs.
        """
        found: list[int] = []
        orig_delay = self._delay
        self._delay = 0.02
        for dev_id in range(1, max_id + 1):
            try:
                self.read_firmware(device_id=dev_id)
                found.append(dev_id)
            except (ValueError, TimeoutError, OSError):
                pass
        self._delay = orig_delay
        return found

    def close(self) -> None:
        """Close the underlying transport connection."""
        self._transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ===========================================================================
# eMS Home HTTP client
# ===========================================================================


class EMSHomeHTTP:
    """
    HTTP client for the ABL eMS Home web interface.

    Authentication uses an OAuth2 "Resource Owner Password" grant against
    ``/api/web-login/token``.  The returned JWT Bearer token is attached to
    every subsequent request via the ``Authorization`` header.  The token is
    valid for 7 days (``expires_in: 604800`` seconds); this class re-acquires
    it automatically when it is about to expire.

    Parameters
    ----------
    host : str
        IP address **or** hostname of the eMS Home unit on your LAN.
        Examples: ``"192.168.1.100"`` or ``"ems-home-12345678"``.
    password : str
        The password shown on the rating plate attached to the unit.
        The username is always ``"admin"`` (hardcoded by the firmware).
    port : int
        HTTP port (default 80).  Change to 443 and set ``use_https=True``
        if your unit has TLS enabled.
    use_https : bool
        Use HTTPS instead of HTTP (default False).
    verify_ssl : bool
        Verify TLS certificate (default False - local devices use self-signed
        certs).
    timeout : float
        Per-request timeout in seconds (default 10).
    """

    # These credentials are hardcoded in the eMS Home firmware.
    _CLIENT_ID = "emos"
    _CLIENT_SECRET = "56951025"
    _USERNAME = "admin"
    _TOKEN_PATH = "/api/web-login/token"

    def __init__(
        self,
        host: str,
        password: str,
        port: int = 80,
        use_https: bool = False,
        verify_ssl: bool = False,
        timeout: float = 10.0,
    ):
        if not _HAS_REQUESTS:
            raise ImportError("requests is required: pip install requests")

        scheme = "https" if use_https else "http"
        self._base = f"{scheme}://{host}:{port}"
        self._password = password
        self._timeout = timeout

        self._session: Session = requests.Session()
        self._session.verify = verify_ssl

        # Token state
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0  # UNIX timestamp

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_token_valid(self) -> bool:
        """Return True if we have a token that won't expire in the next 60 s."""
        return (
            self._access_token is not None and time.time() < self._token_expires_at - 60
        )

    def _apply_auth(self) -> None:
        """Ensure the session has a valid Bearer token, refreshing if needed."""
        if not self._is_token_valid():
            self.login()

    def _get(self, path: str, **kwargs) -> "requests.Response":
        self._apply_auth()
        resp = self._session.get(f"{self._base}{path}", timeout=self._timeout, **kwargs)
        resp.raise_for_status()
        return resp

    def _post(self, path: str, **kwargs) -> "requests.Response":
        self._apply_auth()
        resp = self._session.post(
            f"{self._base}{path}", timeout=self._timeout, **kwargs
        )
        resp.raise_for_status()
        return resp

    def _put(self, path: str, **kwargs) -> "requests.Response":
        self._apply_auth()
        resp = self._session.put(f"{self._base}{path}", timeout=self._timeout, **kwargs)
        resp.raise_for_status()
        return resp

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def login(self) -> dict:
        """
        Obtain a JWT Bearer token via OAuth2 password grant.

        This is called automatically on the first API request, and again
        whenever the token is about to expire.  You only need to call it
        explicitly if you want to verify credentials up front.

        Returns the full token response dict::

            {
                "access_token": "eyJ...",
                "expires_in": 604800,
                "token_type": "Bearer"
            }

        Raises ``requests.HTTPError`` on bad credentials (HTTP 400/401).
        """
        url = f"{self._base}{self._TOKEN_PATH}"
        data = {
            "grant_type": "password",
            "client_id": self._CLIENT_ID,
            "client_secret": self._CLIENT_SECRET,
            "username": self._USERNAME,
            "password": self._password,
        }
        resp = self._session.post(url, data=data, timeout=self._timeout)
        resp.raise_for_status()

        token_data = resp.json()
        self._access_token = token_data["access_token"]
        expires_in = int(token_data.get("expires_in", 604800))
        self._token_expires_at = time.time() + expires_in

        # Attach the token to all future requests in this session
        self._session.headers.update({"Authorization": f"Bearer {self._access_token}"})
        return token_data

    def logout(self) -> None:
        """
        Clear the local token and remove the Authorization header.
        The eMS Home does not have a server-side token-revocation endpoint,
        so this is purely a client-side operation.
        """
        self._access_token = None
        self._token_expires_at = 0.0
        self._session.headers.pop("Authorization", None)

    @property
    def token(self) -> Optional[str]:
        """The current raw JWT access token, or None if not logged in."""
        return self._access_token

    # ------------------------------------------------------------------
    # System information
    # NOTE: The exact API paths below are inferred from common eMS Home
    # firmware patterns.  Verify them via browser DevTools if any return
    # 404 and update the path constants at the top of each method.
    # ------------------------------------------------------------------

    def get_device_status(self) -> DeviceStatus:
        """
        Retrieve a live system-health snapshot from the eMS Home unit.

        Endpoint: GET /api/device-settings/devicestatus  ✓ confirmed

        Returns a ``DeviceStatus`` with CPU load/temp, RAM and flash usage.

        Example::

            s = ems.get_device_status()
            print(s.status)           # "idle"
            print(s.cpu_temp)         # 43  (°C)
            print(s.ram_used_pct)     # 23.3  (%)
        """
        raw = self._get("/api/device-settings/devicestatus").json()
        return DeviceStatus.from_dict(raw)

    # ------------------------------------------------------------------
    # e-mobility  (all endpoints confirmed via DevTools)
    # ------------------------------------------------------------------

    def get_emobility_state(self) -> EMobilityState:
        """
        Retrieve the live e-mobility charging state.

        Endpoint: GET /api/e-mobility/state  ✓ confirmed

        Returns an ``EMobilityState`` with per-phase charging power,
        curtailment setpoint, and overload-protection flag.

        Example::

            state = ems.get_emobility_state()
            print(state.is_charging)       # False
            print(state.total_power_kw)    # 0.0
            print(state.overload_protection_active)  # False
        """
        raw = self._get("/api/e-mobility/state").json()
        return EMobilityState.from_dict(raw)

    def get_charge_mode(self) -> ChargeModeConfig:
        """
        Retrieve the current charge-mode configuration.

        Endpoint: GET /api/e-mobility/config/chargemode  ✓ confirmed

        Example::

            cfg = ems.get_charge_mode()
            print(cfg.mode)               # "lock"
            print(cfg.is_grid_charging)   # False
        """
        raw = self._get("/api/e-mobility/config/chargemode").json()
        return ChargeModeConfig.from_dict(raw)

    def set_charge_mode(
        self,
        mode: str,
        min_charging_power_quota: Optional[int] = None,
        min_pv_power_quota: int = 0,
    ) -> ChargeModeConfig:
        """
        Set the charge mode.

        Endpoint: PUT /api/e-mobility/config/chargemode  ✓ confirmed

        Parameters
        ----------
        mode : str
            Use ``ChargeMode.GRID`` (``"grid"``), ``ChargeMode.LOCK``
            (``"lock"``), or ``"pv"`` for PV-surplus charging.
        min_charging_power_quota : int, optional
            Minimum charging power as a percentage.  Pass ``None`` to leave
            unset (firmware default behaviour).
        min_pv_power_quota : int
            Minimum PV power quota required before charging starts (%).
            Default 0.

        Example::

            ems.set_charge_mode(ChargeMode.GRID)
            ems.set_charge_mode(ChargeMode.LOCK)
            ems.set_charge_mode(ChargeMode.PV,     min_pv_power_quota=100)
            ems.set_charge_mode(ChargeMode.HYBRID, min_pv_power_quota=60)
        """
        if min_pv_power_quota < 0 or min_pv_power_quota > 100:
            raise ValueError(
                f"min_pv_power_quota must be 0-100, got {min_pv_power_quota}"
            )
        payload = {
            "mode": mode,
            "mincharginpowerquota": min_charging_power_quota,
            "minpvpowerquota": min_pv_power_quota,
        }
        self._put("/api/e-mobility/config/chargemode", json=payload)
        return self.get_charge_mode()

    def enable_grid_charging(self) -> ChargeModeConfig:
        """Shortcut: switch to full grid charging."""
        return self.set_charge_mode(ChargeMode.GRID)

    def disable_charging(self) -> ChargeModeConfig:
        """Shortcut: disable charging (lock)."""
        return self.set_charge_mode(ChargeMode.LOCK)

    def enable_pv_charging(self, min_pv_power_quota: int = 100) -> ChargeModeConfig:
        """
        Shortcut: switch to PV-surplus-only charging.

        Parameters
        ----------
        min_pv_power_quota : int
            Minimum PV surplus percentage required before charging starts
            (0-100, default 100).
        """
        return self.set_charge_mode(
            ChargeMode.PV, min_pv_power_quota=min_pv_power_quota
        )

    def enable_hybrid_charging(self, min_pv_power_quota: int = 100) -> ChargeModeConfig:
        """
        Shortcut: switch to hybrid charging (PV surplus + grid top-up).

        Parameters
        ----------
        min_pv_power_quota : int
            Minimum PV surplus percentage before grid top-up kicks in
            (0-100, default 100). The slider in the UI confirmed values
            between 0 and 100 are accepted.
        """
        return self.set_charge_mode(
            ChargeMode.HYBRID,
            min_charging_power_quota=0,
            min_pv_power_quota=min_pv_power_quota,
        )

    def get_ev_parameter_list(self) -> list[EVParameters]:
        """
        Retrieve the list of EVs known to the eMS Home, with their
        current/phase capabilities as detected during the probing sequence.

        Endpoint: GET /api/e-mobility/evparameterlist  ✓ confirmed

        Returns a list of ``EVParameters`` objects (one per UUID).

        Example::

            evs = ems.get_ev_parameter_list()
            for ev in evs:
                print(ev.uuid, ev.max_current, ev.active_phases)
        """
        raw = self._get("/api/e-mobility/evparameterlist").json()
        return [EVParameters.from_dict(uuid, params) for uuid, params in raw.items()]

    # ------------------------------------------------------------------
    # System information
    # ------------------------------------------------------------------

    def get_system_info(self) -> dict:
        """
        Retrieve general system information (firmware version, serial number,
        uptime, network settings, …).

        Endpoint: GET /api/system/info  ← path not yet confirmed via DevTools.
        Use ``explore('/api/system/info')`` to verify, or check the Network
        tab while loading the About/Settings page in the web UI.
        """
        return self._get("/api/system/info").json()

    def get_wallbox_list(self) -> list[dict]:
        """
        Retrieve all eMH1 wallboxes visible on the RS485 bus, with their
        current state, charge current, and energy counters.

        Endpoint: GET /api/devices   (common eMS firmware path)
        """
        return self._get("/api/devices").json()

    def get_wallbox_status(self, device_id: int) -> dict:
        """
        Retrieve the live status of a single wallbox.

        Endpoint: GET /api/devices/{device_id}
        """
        return self._get(f"/api/devices/{device_id}").json()

    def get_energy_data(self) -> dict:
        """
        Retrieve aggregated energy / metering data (total kWh, active power,
        grid feed-in, …).

        Endpoint: GET /api/energy
        """
        return self._get("/api/energy").json()

    # ------------------------------------------------------------------
    # Discovery helper - use this to find the real endpoint paths
    # ------------------------------------------------------------------

    def explore(self, path: str) -> dict | list | str:
        """
        Perform a raw authenticated GET request against any path and return
        the parsed JSON (or raw text on non-JSON responses).

        Use this to discover the actual API surface of your firmware version::

            ems = EMSHomeHTTP("ems-home-12345678", password="xxx")
            print(ems.explore("/api/"))          # may list available routes
            print(ems.explore("/api/devices"))
            print(ems.explore("/api/system/info"))

        Parameters
        ----------
        path : str
            URL path starting with ``/``, e.g. ``"/api/devices"``.
        """
        resp = self._get(path)
        try:
            return resp.json()
        except Exception:
            return resp.text

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def set_wallbox_max_current(self, device_id: int, amps: int) -> dict:
        """
        Set the maximum charge current for a wallbox via the HTTP API.

        Parameters
        ----------
        device_id : int
            Wallbox number as shown in the eMS Home dashboard.
        amps : int
            Desired maximum current (0 to disable, or 6-16 A).

        Endpoint: POST /api/devices/{device_id}/current
        """
        if amps not in (0, *range(6, 17)):
            raise ValueError(f"Invalid current {amps} A - must be 0 or 6..16")
        return self._post(
            f"/api/devices/{device_id}/current",
            json={"max_current": amps},
        ).json()

    def set_load_management(
        self, enabled: bool, max_total_amps: Optional[int] = None
    ) -> dict:
        """
        Enable or disable the eMS Home dynamic load-management feature.

        Parameters
        ----------
        enabled : bool
            True to activate load management.
        max_total_amps : int, optional
            Total current budget shared across all wallboxes.

        Endpoint: POST /api/load_management
        """
        payload: dict = {"enabled": enabled}
        if max_total_amps is not None:
            payload["max_total_current"] = max_total_amps
        return self._post("/api/load_management", json=payload).json()

    # ------------------------------------------------------------------
    # Firmware
    # ------------------------------------------------------------------

    def get_firmware_version(self) -> str:
        """
        Return the firmware version string of the eMS Home unit.
        Falls back to ``get_device_status().status`` if the system/info
        endpoint is not yet confirmed.
        """
        try:
            info = self.get_system_info()
            return info.get("firmware_version", info.get("version", "unknown"))
        except Exception:
            return self.get_device_status().status

    def upload_firmware(self, firmware_path: str) -> dict:
        """
        Upload a ``.bin`` firmware image to the eMS Home for OTA update.

        Endpoint: POST /api/firmware/update
        """
        self._apply_auth()
        with open(firmware_path, "rb") as fh:
            resp = self._session.post(
                f"{self._base}/api/firmware/update",
                files={"firmware": fh},
                timeout=120,
            )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, *_):
        self.logout()


# ===========================================================================
# Convenience wrapper
# ===========================================================================


class ABLEMSHome:
    """
    Unified wrapper combining the HTTP and Modbus interfaces.

    This is the recommended entry point for most use-cases.  It
    uses the eMS Home HTTP API for high-level control and optionally
    a direct Modbus ASCII connection for low-latency polling.

    Parameters
    ----------
    host : str
        LAN IP address of the eMS Home unit.
    password : str
        Web interface password (from the rating plate).
    modbus_serial : str, optional
        Serial port for direct RS485 connection, e.g. '/dev/ttyUSB0'.
    modbus_host : str, optional
        Hostname/IP of an RS485-over-TCP gateway.
    modbus_port : int
        TCP port for the RS485 gateway (default 8899).
    """

    def __init__(
        self,
        host: str,
        password: str,
        modbus_serial: Optional[str] = None,
        modbus_host: Optional[str] = None,
        modbus_port: int = 8899,
    ):
        self.http = EMSHomeHTTP(host, password)
        self.modbus: Optional[EMH1ModbusASCII] = None

        if modbus_serial:
            self.modbus = EMH1ModbusASCII.from_serial(modbus_serial)
        elif modbus_host:
            self.modbus = EMH1ModbusASCII.from_tcp(modbus_host, port=modbus_port)

    def connect(self) -> "ABLEMSHome":
        """Login to the web interface."""
        self.http.login()
        return self

    def disconnect(self) -> None:
        """Logout and close all connections."""
        self.http.logout()
        if self.modbus:
            self.modbus.close()

    def poll_all_wallboxes(self) -> list[CurrentReading]:
        """
        Poll all connected wallboxes via Modbus ASCII and return their
        current state / charging data.  Requires a Modbus connection.
        """
        if not self.modbus:
            raise RuntimeError("No Modbus connection configured")
        device_ids = self.modbus.scan_bus()
        return [self.modbus.read_current(dev_id) for dev_id in device_ids]

    def __enter__(self):
        return self.connect()

    def __exit__(self, *_):
        self.disconnect()


# ===========================================================================
# CLI
# ===========================================================================


def _http_args(p):
    """Attach the shared --host / --password arguments to a subparser."""
    p.add_argument(
        "--host", required=True, help="eMS Home hostname or IP, e.g. ems-home-12345678"
    )
    p.add_argument("--password", required=True, help="Password from the rating plate")
    p.add_argument("--port", type=int, default=80, help="HTTP port (default 80)")


def _modbus_args(p):
    """Attach shared Modbus transport arguments to a subparser."""
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--serial", help="Serial port, e.g. /dev/ttyUSB0")
    g.add_argument("--tcp-host", help="RS485-over-TCP gateway host")
    p.add_argument(
        "--tcp-port", type=int, default=8899, help="TCP gateway port (default 8899)"
    )


def _make_modbus(args) -> EMH1ModbusASCII:
    if args.serial:
        return EMH1ModbusASCII.from_serial(args.serial)
    return EMH1ModbusASCII.from_tcp(args.tcp_host, args.tcp_port)


def _fmt_chargemode(cfg: ChargeModeConfig) -> str:
    lines = [
        f"  Mode              : {cfg.mode}",
        f"  Min charge quota  : {cfg.min_charging_power_quota} %",
        f"  Min PV quota      : {cfg.min_pv_power_quota} %",
        f"  Last charge quota : {cfg.last_min_charging_power_quota} %",
        f"  Last PV quota     : {cfg.last_min_pv_power_quota} %",
    ]
    return "\n".join(lines)


def _fmt_emobility_state(s: EMobilityState) -> str:
    p = s.ev_charging_power
    c = s.curtailment_setpoint
    lines = [
        f"  Charging          : {'yes' if s.is_charging else 'no'}",
        f"  Total power       : {s.total_power_kw} kW",
        f"  Power L1/L2/L3    : {p.l1} / {p.l2} / {p.l3} W",
        f"  Curtailment total : {c.total} W",
        f"  Overload protect  : {'active' if s.overload_protection_active else 'inactive'}",
    ]
    return "\n".join(lines)


def _fmt_device_status(s: DeviceStatus) -> str:
    lines = [
        f"  Status            : {s.status}",
        f"  CPU load          : {s.cpu_load} %",
        f"  CPU temp          : {s.cpu_temp} °C",
        f"  RAM used          : {s.ram_used_pct} %  ({s.ram_free:,} / {s.ram_total:,} bytes free)",
        f"  Flash app used    : {s.flash_app_used_pct} %",
        f"  Flash data used   : {s.flash_data_used_pct} %",
    ]
    return "\n".join(lines)


def _fmt_ev_list(evs: list) -> str:
    if not evs:
        return "  No EVs found."
    lines = []
    for ev in evs:
        phases = ", ".join(ev.active_phases) or "none"
        probed = "yes" if ev.probing_successful else "no"
        lines += [
            f"  UUID     : {ev.uuid}",
            f"  Current  : {ev.min_current}-{ev.max_current} A",
            f"  Phases   : {phases}",
            f"  Probed   : {probed}",
            "",
        ]
    return "\n".join(lines).rstrip()


def _run_smart_meter_cli(host: str, port: int, token: str, timeout: float) -> None:
    """Connect to the smart meter WebSocket, print one reading, then exit."""
    import asyncio
    import base64
    import os
    import struct as _struct
    import datetime

    WS_PATH = "/api/data-transfer/ws/protobuf/gdr/local/values/smart-meter"

    CH = {
        0x100010400FF: ("Grid power total", "W", 1000),
        0x100090400FF: ("Grid apparent power", "W", 1000),
        0x100150400FF: ("Active power L1", "W", 1000),
        0x100290400FF: ("Active power L2", "W", 1000),
        0x1003D0400FF: ("Active power L3", "W", 1000),
        0x1001D0400FF: ("Apparent power L1", "W", 1000),
        0x100310400FF: ("Apparent power L2", "W", 1000),
        0x100450400FF: ("Apparent power L3", "W", 1000),
        0x100200400FF: ("Voltage L1", "V", 1000),
        0x100340400FF: ("Voltage L2", "V", 1000),
        0x100480400FF: ("Voltage L3", "V", 1000),
        0x1001F0400FF: ("Current L1", "A", 1000),
        0x100330400FF: ("Current L2", "A", 1000),
        0x100470400FF: ("Current L3", "A", 1000),
        0x1000E0400FF: ("Frequency", "Hz", 1000),
        0x100010800FF: ("Energy import total", "kWh", 1e6),
    }
    ORDER = [
        "Grid power total",
        "Grid apparent power",
        "Active power L1",
        "Active power L2",
        "Active power L3",
        "Apparent power L1",
        "Apparent power L2",
        "Apparent power L3",
        "Voltage L1",
        "Voltage L2",
        "Voltage L3",
        "Current L1",
        "Current L2",
        "Current L3",
        "Frequency",
        "Energy import total",
    ]

    # --- raw asyncio WS helpers ---

    def _send_ws_text(writer, text: str) -> None:
        """Send a masked WebSocket text frame (masking is required by RFC 6455)."""
        payload = text.encode()
        mask_key = os.urandom(4)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        length = len(payload)
        if length < 126:
            header = bytes([0x81, 0x80 | length])
        else:
            header = bytes([0x81, 0xFE]) + _struct.pack(">H", length)
        writer.write(header + mask_key + masked)

    async def _ws_open():
        reader, writer = await asyncio.open_connection(host, port)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {WS_PATH} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {token}\r\n"
            f"\r\n"
        )
        writer.write(request.encode())
        await writer.drain()
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = await reader.read(4096)
            if not chunk:
                raise ConnectionError("Connection closed during handshake")
            response += chunk
        status = response.split(b"\r\n")[0].decode()
        if "101" not in status:
            raise ConnectionError(f"WebSocket upgrade failed: {status}")
        return reader, writer

    async def _ws_recv(reader):
        header = await reader.readexactly(2)
        opcode = header[0] & 0x0F
        masked = (header[1] & 0x80) != 0
        length = header[1] & 0x7F
        if length == 126:
            length = _struct.unpack(">H", await reader.readexactly(2))[0]
        elif length == 127:
            length = _struct.unpack(">Q", await reader.readexactly(8))[0]
        mask_key = await reader.readexactly(4) if masked else b""
        payload = await reader.readexactly(length)
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:
            raise ConnectionError("Server closed the connection")
        if opcode in (0x9, 0xA):
            return await _ws_recv(reader)
        return payload

    # --- protobuf decoder ---

    def _varint(data, pos):
        result, shift = 0, 0
        while True:
            b = data[pos]
            pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        return result, pos

    def _fields(data):
        pos, out = 0, []
        while pos < len(data):
            try:
                tag, pos = _varint(data, pos)
            except IndexError:
                break
            fn, wt = tag >> 3, tag & 7
            if wt == 0:
                v, pos = _varint(data, pos)
                out.append((fn, wt, v))
            elif wt == 2:
                l, pos = _varint(data, pos)
                out.append((fn, wt, data[pos : pos + l]))
                pos += l
            elif wt == 5:
                v = _struct.unpack_from("<I", data, pos)[0]
                pos += 4
                out.append((fn, wt, v))
            else:
                break
        return out

    def _decode(raw):
        outer = _fields(raw)
        wrap = next((v for fn, wt, v in outer if fn == 1 and wt == 2), None)
        if not wrap:
            return None
        payload = next((v for fn, wt, v in _fields(wrap) if fn == 2 and wt == 2), None)
        if not payload:
            return None
        ts, values = 0.0, {}
        for fn, wt, v in _fields(payload):
            if fn == 3 and wt == 2:
                tsf = _fields(v)
                sec = next((val for f, _, val in tsf if f == 1), 0)
                ns = next((val for f, _, val in tsf if f == 2), 0)
                ts = sec + ns / 1e9
            elif fn == 4 and wt == 2:
                dp = _fields(v)
                ch = next((val for f, _, val in dp if f == 1), None)
                rv = next((val for f, _, val in dp if f == 2), None)
                if ch in CH and rv is not None:
                    label, unit, div = CH[ch]
                    values[label] = (rv / div, unit)
        return ts, values

    # --- main coroutine ---

    async def _fetch():
        reader, writer = await _ws_open()
        # Server requires "Bearer <token>" as the first WS message before it sends data
        _send_ws_text(writer, f"Bearer {token}")
        await writer.drain()
        try:
            msg = await asyncio.wait_for(_ws_recv(reader), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"Error: no frame received within {timeout:.0f}s")
            return
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

        result = _decode(msg)
        if not result:
            print("Error: could not decode frame")
            return
        ts, values = result
        dt = (
            datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            if ts
            else "unknown"
        )
        print(f"Smart meter reading  ({dt})")
        print(f"  {'Measurement':<24} {'Value':>10}  Unit")
        print(f"  {'-'*44}")
        for label in ORDER:
            if label in values:
                val, unit = values[label]
                print(f"  {label:<24} {val:>10.3f}  {unit}")

    asyncio.run(_fetch())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="abl_ems_home",
        description="ABL eMS Home command-line interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
HTTP commands (require --host and --password):
  device-status      Show CPU/RAM/flash health of the eMS Home unit
  charging-state     Show live EV charging power and curtailment
  charge-mode        Show the current charge mode configuration
  set-mode           Change the charge mode
  ev-list            List EVs known to the eMS Home
  explore            Raw authenticated GET against any API path
  smart-meter        Connect to the smart meter WebSocket and print one reading

Modbus commands (require --serial or --tcp-host):
  scan               Scan the RS485 bus for connected wallboxes
  wb-status          Read firmware + live current from a wallbox
  set-current        Set the maximum charge current on a wallbox
""",
    )

    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    # ------------------------------------------------------------------ #
    # HTTP: device-status                                                  #
    # ------------------------------------------------------------------ #
    p = sub.add_parser("device-status", help="Show eMS Home system health")
    _http_args(p)

    # ------------------------------------------------------------------ #
    # HTTP: charging-state                                                 #
    # ------------------------------------------------------------------ #
    p = sub.add_parser("charging-state", help="Show live EV charging state")
    _http_args(p)

    # ------------------------------------------------------------------ #
    # HTTP: charge-mode                                                    #
    # ------------------------------------------------------------------ #
    p = sub.add_parser("charge-mode", help="Show current charge mode")
    _http_args(p)

    # ------------------------------------------------------------------ #
    # HTTP: set-mode                                                       #
    # ------------------------------------------------------------------ #
    p = sub.add_parser("set-mode", help="Set charge mode")
    _http_args(p)
    p.add_argument(
        "mode",
        choices=[ChargeMode.GRID, ChargeMode.LOCK, ChargeMode.PV, ChargeMode.HYBRID],
        help="Charge mode to activate",
    )
    p.add_argument(
        "--pv-quota",
        type=int,
        default=100,
        metavar="PCT",
        help="Min PV surplus %% required before charging (0-100, used with pv/hybrid, default 100)",
    )

    # ------------------------------------------------------------------ #
    # HTTP: ev-list                                                        #
    # ------------------------------------------------------------------ #
    p = sub.add_parser("ev-list", help="List EVs known to the eMS Home")
    _http_args(p)

    # ------------------------------------------------------------------ #
    # HTTP: explore                                                        #
    # ------------------------------------------------------------------ #
    p = sub.add_parser("explore", help="Raw authenticated GET against any API path")
    _http_args(p)
    p.add_argument("path", help="API path to fetch, e.g. /api/e-mobility/state")

    # ------------------------------------------------------------------ #
    # HTTP: smart-meter                                                    #
    # ------------------------------------------------------------------ #
    p = sub.add_parser(
        "smart-meter", help="Print one live smart meter reading via WebSocket"
    )
    _http_args(p)
    p.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        metavar="SEC",
        help="Seconds to wait for first frame (default 10)",
    )

    # ------------------------------------------------------------------ #
    # Modbus: scan                                                         #
    # ------------------------------------------------------------------ #
    p = sub.add_parser("scan", help="Scan RS485 bus for wallboxes")
    _modbus_args(p)

    # ------------------------------------------------------------------ #
    # Modbus: wb-status                                                    #
    # ------------------------------------------------------------------ #
    p = sub.add_parser("wb-status", help="Read firmware + current from a wallbox")
    _modbus_args(p)
    p.add_argument("--id", type=int, default=1, help="Wallbox device ID (default 1)")

    # ------------------------------------------------------------------ #
    # Modbus: set-current                                                  #
    # ------------------------------------------------------------------ #
    p = sub.add_parser("set-current", help="Set max charge current on a wallbox")
    _modbus_args(p)
    p.add_argument("--id", type=int, required=True, help="Wallbox device ID")
    p.add_argument(
        "--amps", type=int, required=True, help="Max current in amps (0 or 6-16)"
    )

    # ------------------------------------------------------------------ #
    # Dispatch                                                             #
    # ------------------------------------------------------------------ #
    args = parser.parse_args()

    if args.cmd is None:
        parser.print_help()
        raise SystemExit(0)

    HTTP_CMDS = {
        "device-status",
        "charging-state",
        "charge-mode",
        "set-mode",
        "ev-list",
        "explore",
        "smart-meter",
    }
    MODBUS_CMDS = {"scan", "wb-status", "set-current"}

    if args.cmd in HTTP_CMDS:
        ems = EMSHomeHTTP(args.host, args.password, port=args.port)
        try:
            ems.login()
        except Exception as exc:
            print(f"Login failed: {exc}")
            raise SystemExit(1)

        try:
            if args.cmd == "device-status":
                print("Device status:")
                print(_fmt_device_status(ems.get_device_status()))

            elif args.cmd == "charging-state":
                print("e-Mobility charging state:")
                print(_fmt_emobility_state(ems.get_emobility_state()))

            elif args.cmd == "charge-mode":
                print("Charge mode configuration:")
                print(_fmt_chargemode(ems.get_charge_mode()))

            elif args.cmd == "set-mode":
                if args.mode in (ChargeMode.PV, ChargeMode.HYBRID):
                    cfg = ems.set_charge_mode(
                        args.mode, min_pv_power_quota=args.pv_quota
                    )
                else:
                    cfg = ems.set_charge_mode(args.mode)
                print("Charge mode updated:")
                print(_fmt_chargemode(cfg))

            elif args.cmd == "ev-list":
                evs = ems.get_ev_parameter_list()
                print(f"EVs known to eMS Home ({len(evs)} found):")
                print(_fmt_ev_list(evs))

            elif args.cmd == "explore":
                import json as _json

                result = ems.explore(args.path)
                print(
                    _json.dumps(result, indent=2)
                    if isinstance(result, (dict, list))
                    else result
                )

            elif args.cmd == "smart-meter":

                _run_smart_meter_cli(args.host, args.port, ems.token, args.timeout)

        except Exception as exc:
            print(f"Error: {exc}")
            raise SystemExit(1)
        finally:
            ems.logout()

    elif args.cmd in MODBUS_CMDS:
        try:
            wb = _make_modbus(args)
        except Exception as exc:
            print(f"Failed to open Modbus connection: {exc}")
            raise SystemExit(1)

        with wb:
            if args.cmd == "scan":
                ids = wb.scan_bus()
                if ids:
                    print(f"Found {len(ids)} wallbox(es): device IDs {ids}")
                else:
                    print("No wallboxes found on the bus.")

            elif args.cmd == "wb-status":
                fw = wb.read_firmware(args.id)
                cr = wb.read_current(args.id)
                print(f"Wallbox device {args.id}:")
                print(
                    f"  Firmware  : v{fw.firmware_major}.{fw.firmware_minor} ({fw.hardware_revision})"
                )
                print(f"  State     : {cr.state.name} (0x{cr.state.value:02X})")
                print(f"  Icmax     : {cr.max_current_amps} A")
                print(f"  Phase 1   : {cr.phase1_amps} A")
                print(f"  Phase 2   : {cr.phase2_amps} A")
                print(f"  Phase 3   : {cr.phase3_amps} A")
                print(f"  Power     : ~{cr.total_power_kw} kW")

            elif args.cmd == "set-current":
                wb.set_max_current(args.id, args.amps)
                print(f"Wallbox {args.id}: max current set to {args.amps} A")
