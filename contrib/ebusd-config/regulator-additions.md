# Additional registers for regulator decisions (field-verified)

These lines make decisions from the sensoCOMFORT (ctlv3) and the heat pump
(HMU) visible as HA entities. Each one is cross-checked on the real device via
raw reads (`ebusctl hex …`) -- no guessed offsets.

Append to the CSV named in each section, in the ebusd config folder, then
restart ebusd. The eBUS Heating Control integration then creates the entities
on its own.

## 1) Domestic hot water status code, readable -- in `38.v32.csv`
Append the value table DIRECTLY to the existing `Statenumber` line; NO second
message on the same register 0dab00 (ebusd rejects duplicates -> loads
silently without it). 24 = domestic hot water, field-verified (14.45 kW +
57 °C flow observed simultaneously with Statenumber 31→24). Then shows plain
text instead of a number; automations match on the tokens.

## 2) Heat pump backup-heater release -- `08.hmu.HW5103.csv`
`releasebackup` is bit 1 of byte 7 of the SetMode write message (b510/00).
Passively overheard (`u`), no active bus read. Layout from `find -f`:
byte0 hcmode · 1 flowtemp · 2 hwctemp · 3 hwcflowtemp · 4 IGN · 5 disable bits ·
6 IGN · 7 {remotecontrolhcpump=0, releasebackup=1, releasecooling=2}.

OPEN: verify the exact file column format of 08.hmu before finalizing the
line (don't copy the `find -f` format as-is). It also sits on b510/00 like
SetMode -- whether ebusd allows a second message on the same register still
needs testing (Statenumber/StatenumberText has shown: duplicates are
rejected). The field's bit position is confirmed: bit 1 of byte 7
(`IGN:7` + `BI1:1`).

Caveat: this is the heat pump's *internal* backup-heater release -- not
necessarily the signal for the external eloBlock (address 38), which the
regulator drives directly via its FlowTempDemand.

## 3) Legionella protection time + day -- `15.ctlv3.csv`
ID scheme verified against `HwcTempDesired` (`@base 0x24,0x2,RW,BLOCK,SUB` +
`@ext REG` → b524 ID `02 RW BLOCK SUB REG 00`). Raw read confirms the layout
`IGN:4` (echo) + value:

- Time `0x2a`: response `07 0300 2a00 04 00 00` → HTI `04:00:00`
- Day  `0x2b`: response `06 0300 2b00 00 00`   → daysel 0 = **off** (protection disabled)

IMPORTANT: write in the FILE format (empty circuit AND level columns, empty
part), NOT in `ebusctl find -f` format. `find -f` omits the level column and
shows circuit/zz explicitly -> copying it as-is shifts every column and ebusd
silently discards the line. Use a real file line as a template (e.g.
`ContinuousHeating` in 15.ctlv3.csv).

```csv
r,,,HwcLegionellaTime,Legionella protection time,,,b524,020000002a00,ign,,IGN:4,,,,value,,HTI,,,
r,,,HwcLegionellaDay,Legionella protection weekday,,,b524,020000002b00,ign,,IGN:4,,,,value,,UIN,0=off;1=Mon;2=Tue;3=Wed;4=Thu;5=Fri;6=Sat;7=Sun;8=daily,,
```

Daysel table: 0=off, 1..7=Mon..Sun, 8=daily. State at the time of writing:
**off** -- legionella protection is not activated, so it was not the cause of
the electrical domestic hot water charging.
