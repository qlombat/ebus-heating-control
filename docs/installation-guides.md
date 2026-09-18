# Installation guides

Step-by-step, "how to" guides for setting this integration up on a specific
real installation — physical bus wiring through to a working setup. Written as
instructions you can follow, not just a retrospective; use the exact values
where they apply to your hardware, otherwise adapt as needed. See the main
[README](../README.md) for general reference documentation (this is not
required reading to use the integration).

Have a working installation with a different boiler, controller, or adapter?
Feel free to open a PR adding a new guide below, following the same structure.

> ### ⚠️ Read before you touch your boiler
> These guides involve **disconnecting or removing your boiler's factory-fitted
> room controller** and letting Home Assistant write control commands directly
> to the heating system over eBUS. Before you start:
>
> - **Work on gas/oil heating equipment carries real risk** (electrical, gas,
>   scalding, carbon monoxide) if done incorrectly. If you're not confident
>   working on your boiler's wiring, hire a qualified heating engineer/electrician.
>   Always de-power the boiler before connecting or disconnecting anything on
>   its control bus.
> - **This may void your boiler's warranty** and, depending on where you live,
>   may not be legal to do yourself on a gas appliance — check your local
>   regulations and your warranty terms first.
> - **Removing the manufacturer's controller removes its safety/comfort
>   logic**, not just its UI. Test thoroughly (as in [Step 5](#step-5--test-a-manual-setmode-write)
>   below) before relying on this for real heating, especially before winter,
>   and keep a plan for how to heat your home if Home Assistant, ebusd, or the
>   network is down.
> - **This integration and the people contributing to it take no
>   responsibility for damage, injury, voided warranties, non-compliance with
>   local regulations, or heating outages resulting from following these
>   guides.** You are making changes to your own heating system entirely at
>   your own risk — see the [LICENSE](../LICENSE) ("AS IS", no warranty).

## Contents
- [Bulex boiler (Vaillant BAI) — replacing a wireless Exacontrol E7](#bulex-boiler-vaillant-bai--replacing-a-wireless-exacontrol-e7)

## Bulex boiler (Vaillant BAI) — replacing a wireless Exacontrol E7

**Target hardware:** a Bulex gas boiler (Bulex is a Belgian brand within the
Vaillant Group — expect ebusd to identify it as a Vaillant `BAI`-circuit unit,
that's correct, not a misdetection) with a wireless **Exacontrol E7** room
controller to be fully replaced.

### Step 1 — Remove the Exacontrol from the bus

Physically disconnect/depower the Exacontrol E7. Don't leave it powered on the
bus alongside an eBUS adapter "just in case" — having two active regulators on
the bus at the same time caused issues in testing. Only one regulator should
be active on the bus; Home Assistant takes over that role from here on.

### Step 2 — Wire up a Wi-Fi eBUS adapter

Connect an ESP-based Wi-Fi eBUS adapter (a popular DIY design, sometimes
referred to as the "Daniel Kucera adapter") directly to the boiler's two eBUS
wires, and join it to your local network. No relay, dry contact, or ON/OFF
module is needed — that's the whole point of doing this over eBUS instead:
the boiler stays fully modulating.

Target chain once this step is done:

```
Bulex boiler (eBUS)
        │
        ▼
 Wi-Fi eBUS adapter ──── enh: protocol over TCP :3335
        │
        ▼
      ebusd
        │
        ▼
Home Assistant (this integration)
```

### Step 3 — Configure and start the ebusd add-on

Install the [ebusd add-on](https://github.com/LukasGrebe/ha-addons) and set
its `commandline_options` to:

```
--device=enh:192.168.40.7:3335
--scanconfig
--httpport=8889
--pollinterval=30
--accesslevel=install
```

Replace `192.168.40.7:3335` with your own adapter's IP and port. Keep
`--accesslevel=install` — it's required later for writing `SetMode` (see
[Step 5](#step-5--test-a-manual-setmode-write)); without it ebusd rejects the
write outright (see the README's [Requirements](../README.md#requirements)).

Start the add-on, then check its log for a successful scan:

```
scan 08: ;Vaillant;BAI00;0701;3302
read scan config file vaillant/08.bai.csv
found messages: 182
```

That confirms manufacturer `Vaillant`, model `BAI00`, software version `0701`,
hardware version `3302`, at eBUS address `08`, with 182 messages loaded from
the stock `vaillant/08.bai.csv` definition file. If your log doesn't show a
successful scan yet, don't move on to the next step — fix that first (check
the adapter's IP/port and physical wiring).

### Step 4 — Install the integration

Install via HACS as described in the README's
[Installation](../README.md#installation) section, then add it from
**Settings → Devices & services**. The host/port fields are pre-filled with
sane defaults — no extra configuration is needed at this stage. No MQTT broker
is involved anywhere in this chain: reads go over ebusd's HTTP-JSON API
(`8889`), writes over its TCP command port (`8888`).

This fork already includes the Home Assistant compatibility fixes and the
`lastup`/`passive` discovery fixes needed for entities to show up promptly on
a recent Home Assistant — nothing to patch yourself. (For reference, without
them: entity discovery would silently stall at a handful of `Currenterror`
entities, and ebusd's log would fill up with `ERR: end of input reached` every
polling cycle. See [CHANGELOG.md](../CHANGELOG.md) if you want the details.)

### Step 5 — Test a manual `SetMode` write

Before configuring the automatic
[boiler regulation](../README.md#boiler-regulation), confirm the write path
works by calling the
[`ebus_heating_control.write`](../README.md#service-ebus_heating_controlwrite)
service manually, from **Developer tools → Actions**:

```yaml
action: ebus_heating_control.write
data:
  circuit: bai
  message: SetMode
  value: "auto;30;-;-;0;0;0;0;0;0"
```

`SetMode`'s ten `;`-separated fields, in order:

| # | Field | Value used | Meaning |
|---|---|---|---|
| 1 | `hcmode` | `auto` | Let the boiler electronics decide heating vs. DHW; still honors the flow setpoint below. |
| 2 | `flowtempdesired` | `30` | Requested flow (departure) temperature, °C. |
| 3 | `hwctempdesired` | `-` | DHW setpoint — left unchanged. |
| 4 | `hwcflowtempdesired` | `-` | DHW flow setpoint — left unchanged. |
| 5 | `disablehc` | `0` | Heating **not** disabled. |
| 6 | `disablehwctapping` | `0` | DHW tapping **not** disabled. |
| 7 | `disablehwcload` | `0` | DHW loading **not** disabled. |
| 8 | `remotecontrolhcpump` | `0` | No forced pump control. |
| 9 | `releasebackup` | `0` | No backup heater release. |
| 10 | `releasecooling` | `0` | No cooling release. |

Then check `FlowTempDesired` (e.g. via ebusd's `http://<host>:8889/data/bai/FlowTempDesired?exact=1&required=1&maxage=0`,
or the corresponding `sensor` entity). On this installation it read
`60.00 °C` beforehand (the boiler's own default/last setpoint) and `30.00 °C`
right after the write, staying there — confirming the full path actually
works, not just that the write was accepted:

```
Home Assistant → ebus_heating_control.write → ebusd → Wi-Fi eBUS adapter
→ eBUS → boiler → FlowTempDesired = 30 °C
```

If your reading doesn't change, double-check `--accesslevel=install` is set
(Step 3) and that `circuit`/`message` match what ebusd's `/data` endpoint
shows for your device.

### Step 6 — Turn the manual test into automatic regulation

At this point Home Assistant can already read flow/return temperature,
pressure, modulation percentage, flame/pump/heating/DHW switch states, and
write an arbitrary `SetMode` by hand — but the boiler still needs a human (or
an automation) to decide *what* setpoint to send and *when*.

To make that automatic, enable the built-in
[boiler regulation](../README.md#boiler-regulation): go to
**Settings → Devices & services → eBUS Heating Control → Configure**, and set
`boiler_room_sensor` (required) and, optionally, `boiler_outdoor_sensor`. This
creates a `climate` entity that continuously computes `flowtempdesired` from
those sensors (plus an optional heating curve), applies hysteresis so it
actually stops calling for heat once the room is warm enough, and can follow a
[weekly time/temperature schedule](../README.md#weekly-heating-schedule) if you
add one — all while keeping `hcmode` at `auto` so DHW production stays
completely unaffected by the heating on/off state. No more manual `SetMode`
writes needed after this.
