"""Reine Regelungslogik für die modulierende Kessel-Vorlaufsollwert-Berechnung.

Bewusst ohne jeden Home-Assistant-Import (wie `model.py`), damit sie isoliert
und ohne die `homeassistant`-Abhängigkeit getestet werden kann (siehe
`tests/test_regulation.py`).

Bildet nach, was ein außentemperaturgeführtes Raumthermostat (z. B. das
ersetzte Exacontrol) tut, aber transparent und einstellbar:

    Vorlauf-Basis (Heizkurve) = Raum-Soll + Steigung * (Raum-Soll - Außentemp)
    Korrektur (PI)            = Kp * Raumfehler + Integral
    Vorlauf-Sollwert          = clamp(Basis + Korrektur, flow_min, flow_max)

Der Vorlauf-Sollwert wird anschließend über die eBUS-Kommando `SetMode` des
BAI-Kreises geschrieben (siehe `format_setmode`); das ist der einzige Weg,
`flowtempdesired` modulierend zu setzen (Einzelfeld-Schreiben ist bei
Mehrfeld-Kommandos wie `SetMode` von ebusd nicht vorgesehen).
"""
from __future__ import annotations

from dataclasses import dataclass

# SetMode kennt laut Vaillant-Protokoll (hcmode-Template) 0=auto;1=off;2=heat;3=water.
# "auto" lässt die Kesselelektronik selbst entscheiden, ob Heizung oder WW bedient
# wird, respektiert aber den mitgeschickten Vorlauf-Sollwert -- am echten Gerät
# validiert (FlowTempDesired 60 °C -> 30 °C). Bewusst NIE "off": das ist der
# grobe Gesamt-Modus des Kessels und schaltet vermutlich auch die WW-Bereitung
# ab, was dem Ziel widerspricht, ECS unabhängig vom Heizungs-Ein/Aus dieser
# Entity zu halten. Heizung an/aus läuft stattdessen ausschließlich über das
# granulare, eindeutig benannte `disablehc`-Bit (siehe `format_setmode`).
HCMODE_ACTIVE = "auto"


@dataclass(frozen=True)
class RegulationParams:
    """Einstellbare Regelparameter (kommen aus dem Options-Flow)."""

    curve_slope: float
    kp: float
    ki: float
    flow_min: float
    flow_max: float
    # Begrenzung des Integralanteils allein (Anti-Windup) -- unabhängig von
    # flow_min/flow_max, damit ein bereits gesättigter Vorlauf den Integral-
    # Zustand nicht unbegrenzt weiter aufsummiert.
    integral_limit: float = 10.0
    # Hysterese der Wärmeanforderung (°C): erst AUS, wenn der Raum die Konsigne
    # um mehr als diesen Wert überschreitet (siehe `should_call_for_heat`).
    # Ohne das würde die Kesselregelung nie wirklich stoppen -- die Heizkurve
    # allein liefert auch bei erreichter Konsigne einen Vorlauf > 0.
    hysteresis: float = 0.3
    # Vorlauf-Basis, wenn kein Außensensor konfiguriert ist (dann entfällt die
    # Heizkurve komplett -- reine Raum-PI-Regelung um diesen Basiswert).
    base_flow: float = 35.0


@dataclass(frozen=True)
class RegulationResult:
    flow_setpoint: float
    integral: float
    error: float


def should_call_for_heat(
    target_room: float,
    current_room: float,
    currently_calling: bool,
    hysteresis: float,
) -> bool:
    """Hysterese-Wärmeanforderung: verhindert Kurzzyklen um die Konsigne.

    Startet die Anforderung sobald der Raum unter die Konsigne fällt, beendet
    sie erst, wenn er sie um `hysteresis` überschreitet -- ohne diese Sperre
    würde `compute_flow_setpoint` (reine Heizkurve) auch bei erreichter
    Konsigne dauerhaft einen Vorlauf > 0 liefern und der Kessel liefe endlos.
    """
    if currently_calling:
        return current_room < target_room + hysteresis
    return current_room < target_room


def compute_flow_setpoint(
    target_room: float,
    current_room: float,
    outdoor_temp: float | None,
    integral: float,
    params: RegulationParams,
) -> RegulationResult:
    """Berechnet Vorlauf-Sollwert und neuen Integral-Zustand für einen Zyklus.

    `outdoor_temp=None` (kein Außensensor konfiguriert): die Heizkurve entfällt,
    `params.base_flow` dient als fester Ausgangspunkt -- reine Raum-PI-Regelung.
    Weniger vorausschauend als mit Wetterkompensation, aber funktioniert auch
    ohne zweiten Sensor.

    Rein und seiteneffektfrei: `integral` kommt vom Aufrufer (z. B. über
    `RestoreEntity` persistiert) und wird als Teil des Ergebnisses zurückgegeben,
    statt hier gespeichert zu werden.
    """
    error = target_room - current_room
    if outdoor_temp is None:
        curve_base = params.base_flow
    else:
        curve_base = target_room + params.curve_slope * (target_room - outdoor_temp)
    integral = max(
        -params.integral_limit,
        min(params.integral_limit, integral + error * params.ki),
    )
    raw_flow = curve_base + params.kp * error + integral
    flow = max(params.flow_min, min(params.flow_max, raw_flow))
    return RegulationResult(flow_setpoint=round(flow, 1), integral=round(integral, 3), error=error)


def format_setmode(flow_setpoint: float | None, calling_for_heat: bool) -> str:
    """Baut den 10-Felder-Wertestring für das eBUS-Kommando `SetMode`.

    `hcmode` bleibt IMMER `"auto"` -- auch wenn der Nutzer die Heizung über
    diese Entity ausgeschaltet hat oder die Konsigne erreicht ist (Hysterese,
    siehe `should_call_for_heat`). Nur das granulare `disablehc`-Bit sperrt in
    dem Fall die Heizfunktion; die Warmwasserbereitung (`HwcSwitch`,
    `hwctempdesired`) bleibt davon komplett unberührt. `hcmode="off"` würde
    dagegen den gesamten Kessel-Modus umschalten und vermutlich auch die
    WW-Bereitung mit abschalten -- genau das soll diese Entity nicht tun.

    Nur `hcmode`, `flowtempdesired` und `disablehc` werden gesetzt (`-` = un-
    verändert lassen); die restlichen Felder bleiben 0 (keine weiteren Sperren/
    Freigaben angefordert) -- entspricht dem am realen Gerät validierten
    Testwert `auto;30;-;-;0;0;0;0;0;0`.
    """
    if not calling_for_heat or flow_setpoint is None:
        return f"{HCMODE_ACTIVE};-;-;-;1;0;0;0;0;0"
    return f"{HCMODE_ACTIVE};{flow_setpoint};-;-;0;0;0;0;0;0"
