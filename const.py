"""Constants for the ABL eMS Home integration."""

DOMAIN = "abl_ems_home"

# Config entry keys
CONF_HOST           = "host"
CONF_PASSWORD       = "password"
CONF_PORT           = "port"
CONF_SCAN_INTERVAL  = "scan_interval"

# Defaults
DEFAULT_PORT          = 80
DEFAULT_SCAN_INTERVAL = 30   # seconds

# Coordinator update key stored in hass.data
DATA_COORDINATOR = "coordinator"
