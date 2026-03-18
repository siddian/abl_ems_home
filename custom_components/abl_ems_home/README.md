# ABL eMS Home – Home Assistant Integration

A custom integration for the **ABL eMS Home** energy management system.
Provides sensors for live charging state and device health, plus controls
to change the charge mode directly from the Home Assistant UI.

---

## Installation

### HACS (recommended)
1. Open HACS → Integrations → ⋮ → Custom repositories
2. Add the URL of this repository, category **Integration**
3. Search for "ABL eMS Home" and install
4. Restart Home Assistant

### Manual
1. Copy the `abl_ems_home/` folder into your
   `config/custom_components/` directory so the path looks like:
   ```
   config/
   └── custom_components/
       └── abl_ems_home/
           ├── __init__.py
           ├── abl_ems_home.py
           ├── config_flow.py
           ├── coordinator.py
           ├── const.py
           ├── manifest.json
           ├── select.py
           ├── sensor.py
           ├── strings.json
           └── translations/
               └── en.json
   ```
2. Restart Home Assistant.

---

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **ABL eMS Home**
3. Fill in:
   | Field | Description |
   |---|---|
   | **Host** | IP address or hostname of the unit, e.g. `ems-home-12345678` or `192.168.1.100` |
   | **Password** | Password printed on the rating plate of the unit |
   | **HTTP port** | Usually `80` (default) |
   | **Poll interval** | How often to refresh data in seconds (10–300, default 30) |

The integration will verify your credentials before saving.

---

## Entities

### Sensors (always enabled)
| Entity | Description | Unit |
|---|---|---|
| `sensor.abl_ems_home_ev_charging_power` | Total EV charging power | W |
| `sensor.abl_ems_home_charge_mode` | Active charge mode (`lock`, `grid`, `pv`, `hybrid`) | — |
| `sensor.abl_ems_home_min_pv_power_quota` | Current PV surplus quota setting | % |
| `sensor.abl_ems_home_device_status` | Unit status (`idle`, `charging`, …) | — |

### Sensors (disabled by default, enable in entity registry)
| Entity | Description |
|---|---|
| `sensor.abl_ems_home_ev_charging_power_l1/l2/l3` | Per-phase charging power |
| `sensor.abl_ems_home_curtailment_setpoint` | Load management curtailment |
| `sensor.abl_ems_home_cpu_load` | CPU load % |
| `sensor.abl_ems_home_cpu_temp` | CPU temperature °C |
| `sensor.abl_ems_home_ram_usage` | RAM utilisation % |
| `sensor.abl_ems_home_flash_data_usage` | Flash data partition usage % |

### Controls
| Entity | Description |
|---|---|
| `select.abl_ems_home_charge_mode` | Dropdown to switch charge mode |
| `number.abl_ems_home_min_pv_power_quota` | Slider for PV surplus quota (0–100 %) |

---

## Automations

### Switch to PV charging when solar is producing
```yaml
automation:
  - alias: "EV – switch to PV charging at sunrise"
    trigger:
      - platform: sun
        event: sunrise
    action:
      - service: select.select_option
        target:
          entity_id: select.abl_ems_home_charge_mode
        data:
          option: pv
```

### Lock charging at night
```yaml
automation:
  - alias: "EV – lock charging at night"
    trigger:
      - platform: time
        at: "22:00:00"
    action:
      - service: select.select_option
        target:
          entity_id: select.abl_ems_home_charge_mode
        data:
          option: lock
```

### Set PV quota dynamically
```yaml
action:
  - service: number.set_value
    target:
      entity_id: number.abl_ems_home_min_pv_power_quota
    data:
      value: 60
```

---

## Supported charge modes

| Mode | Description |
|---|---|
| `lock` | Charging disabled |
| `grid` | Charge at full grid power |
| `pv` | Charge only from PV surplus |
| `hybrid` | PV surplus + grid top-up |
