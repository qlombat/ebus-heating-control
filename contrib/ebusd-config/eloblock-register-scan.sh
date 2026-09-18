#!/bin/sh
# eloBlock (VE24, address 38 behind the V32 coupler) -- b509 register scan.
#
# Reads 0d<RR>00 for RR = START..END raw via 'ebusctl hex' and prints one line
#   0d<RR>00 = <response-hex or ERR>
# per register. Purpose: find the eloBlock's actual operating counters. The
# d.80..d.83 (0d2800/2900/2200/2300) taken from the community list return a
# constant 0 on this VE24 and were commented out in 38.v32.csv.
#
# Method -- before/after diff:
#   sh eloblock-register-scan.sh > scanA.txt          # now (idle state)
#   ... let the eloBlock run a DHW/heating cycle ...
#   sh eloblock-register-scan.sh > scanB.txt          # afterwards
#   diff scanA.txt scanB.txt
# Registers whose value in B is greater than in A are the counters
# (hours: +1..2, starts: +1). Send both files -- I'll decode them.
#
# Optionally narrow the range (decimal): sh eloblock-register-scan.sh 0 128
#
# Notes:
# - Run this where 'ebusctl' runs (the ebusd add-on terminal).
# - Read only (0d = read command), harmless.
# - Non-existing registers run into the read timeout -> a full run
#   (0..255) can take several minutes. Best done during a quiet phase.
# - </dev/null per call prevents ebusctl from consuming the loop's stdin.
START=${1:-0}
END=${2:-255}
SUBS=${3:-00 01}   # both known sub-bytes: 00 (standard) and 01 (d.100..)
r=$START
while [ "$r" -le "$END" ]; do
  hx=$(printf '%02x' "$r")
  for s in $SUBS; do
    printf '0d%s%s = %s\n' "$hx" "$s" \
      "$(ebusctl hex 38b509030d${hx}${s} </dev/null 2>&1)"
  done
  r=$((r + 1))
done
