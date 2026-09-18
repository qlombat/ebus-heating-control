"""Constants for the eBUS Heating Control integration."""

DOMAIN = "ebus_heating_control"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_HTTP_PORT = "http_port"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_EXCLUDE = "exclude"
CONF_FAST = "fast"

# Boiler modulation regulation (weather compensation + room PI), optional --
# only active when a room and outdoor sensor are configured (see climate.py).
CONF_BOILER_ROOM_SENSOR = "boiler_room_sensor"
CONF_BOILER_OUTDOOR_SENSOR = "boiler_outdoor_sensor"
CONF_BOILER_CURVE_SLOPE = "boiler_curve_slope"
CONF_BOILER_KP = "boiler_kp"
CONF_BOILER_KI = "boiler_ki"
CONF_BOILER_FLOW_MIN = "boiler_flow_min"
CONF_BOILER_FLOW_MAX = "boiler_flow_max"
CONF_BOILER_WRITE_INTERVAL = "boiler_write_interval"
CONF_BOILER_HYSTERESIS = "boiler_hysteresis"
CONF_BOILER_BASE_FLOW = "boiler_base_flow"

DEFAULT_PORT = 8888  # TCP command port (write)
DEFAULT_HTTP_PORT = 8889  # HTTP-JSON port (read/definitions)
DEFAULT_SCAN_INTERVAL = 30
DEFAULT_EXCLUDE = "Timer"  # hide time programs by default (a lot of noise)
# These messages are read directly from the bus every cycle (required+maxage),
# instead of waiting for ebusd's poll rotation. Keep this list small: every
# entry costs one bus read per scan_interval.
DEFAULT_FAST = ""

# Defaults tuned for radiators; for underfloor heating pick significantly
# lower values (e.g. slope 0.3-0.6, flow 25-35 °C). Calibrated against a real
# Exacontrol replacement: classic radiators previously ran at roughly 60 °C
# flow at 0 °C outside and a 22 °C room target -> slope ≈ 1.7
# (60 = 22 + 1.7*(22-0)). Radiators with thermostatic valves also need a
# noticeably higher minimum flow temperature than underfloor heating,
# otherwise they emit practically no heat in mild weather even though the
# boiler is modulating correctly.
DEFAULT_BOILER_CURVE_SLOPE = 1.7
DEFAULT_BOILER_KP = 3.0
DEFAULT_BOILER_KI = 0.1
DEFAULT_BOILER_FLOW_MIN = 40.0
DEFAULT_BOILER_FLOW_MAX = 65.0
DEFAULT_BOILER_WRITE_INTERVAL = 60
# How far the setpoint may be exceeded before the heat demand stops (see
# regulation.should_call_for_heat) -- prevents short-cycling.
DEFAULT_BOILER_HYSTERESIS = 0.3
# Fixed base flow temperature when no outdoor sensor is configured (see
# regulation.compute_flow_setpoint) -- a moderate radiator flow starting
# point, within [DEFAULT_BOILER_FLOW_MIN, DEFAULT_BOILER_FLOW_MAX].
DEFAULT_BOILER_BASE_FLOW = 45.0
