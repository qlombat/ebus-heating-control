"""Konstanten für die eBUS-Bridge-Integration."""

DOMAIN = "ebus_bridge"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_HTTP_PORT = "http_port"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_EXCLUDE = "exclude"
CONF_FAST = "fast"

# Kessel-Modulationsregelung (Wetterkompensation + Raum-PI), optional --
# nur aktiv, wenn Raum- und Außensensor konfiguriert sind (siehe climate.py).
CONF_BOILER_ROOM_SENSOR = "boiler_room_sensor"
CONF_BOILER_OUTDOOR_SENSOR = "boiler_outdoor_sensor"
CONF_BOILER_CURVE_SLOPE = "boiler_curve_slope"
CONF_BOILER_KP = "boiler_kp"
CONF_BOILER_KI = "boiler_ki"
CONF_BOILER_FLOW_MIN = "boiler_flow_min"
CONF_BOILER_FLOW_MAX = "boiler_flow_max"
CONF_BOILER_WRITE_INTERVAL = "boiler_write_interval"
CONF_BOILER_HYSTERESIS = "boiler_hysteresis"

DEFAULT_PORT = 8888  # TCP-Kommandoport (Schreiben)
DEFAULT_HTTP_PORT = 8889  # HTTP-JSON-Port (Lesen/Definitionen)
DEFAULT_SCAN_INTERVAL = 30
DEFAULT_EXCLUDE = "Timer"  # Zeitprogramme standardmäßig ausblenden (viel Rauschen)
# Diese Nachrichten werden je Zyklus direkt vom Bus gelesen (required+maxage),
# statt auf ebusds Poll-Umlauf zu warten. Klein halten: jeder Eintrag kostet
# einen Bus-Read je scan_interval.
DEFAULT_FAST = ""

# Startwerte passend für Heizkörper (Radiatoren); für Fußbodenheizung deutlich
# niedriger wählen (z. B. Steigung 0.3-0.6, Vorlauf 25-35 °C).
DEFAULT_BOILER_CURVE_SLOPE = 1.2
DEFAULT_BOILER_KP = 3.0
DEFAULT_BOILER_KI = 0.1
DEFAULT_BOILER_FLOW_MIN = 30.0
DEFAULT_BOILER_FLOW_MAX = 55.0
DEFAULT_BOILER_WRITE_INTERVAL = 60
# Wie weit die Konsigne überschritten werden darf, bevor die Wärmeanforderung
# stoppt (siehe regulation.should_call_for_heat) -- verhindert Kurzzyklen.
DEFAULT_BOILER_HYSTERESIS = 0.3
