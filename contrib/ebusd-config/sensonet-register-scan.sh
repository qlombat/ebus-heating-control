#!/bin/sh
# Vaillant sensoNET VR 921 (NETX3, address f6) -- b509 register scan.
#
# Purpose: empirically check whether the sensoNET AT ADDRESS f6 has any
# readable registers at all, before blindly reusing other devices'
# definitions. First direct measurements (see f6.netx3.csv) only returned
# empty acknowledgments on b509/b511/b504 -- the sensoNET is a cloud gateway,
# not a data-carrying device. This script disproves or confirms that across
# the full b509 range.
#
# Output per line:  0d<RR><ss> = <response-hex or ERR>
#   0x00 with length byte 00  -> register exists, but EMPTY (no value)
#   length > 0 with real bytes -> CANDIDATE: send it to me, I'll decode it
#   ERR / timeout               -> register doesn't exist
#
# Usage (in the ebusd add-on terminal, where 'ebusctl' runs):
#   sh sensonet-register-scan.sh > f6scan.txt
#   ... send the file ...
# Narrow the range (decimal):  sh sensonet-register-scan.sh 0 128
#
# Notes:
# - Read only (0d = read command), harmless.
# - f6 is a very active master; non-existing registers run into the read
#   timeout -> a full run (0..255 x 2 subs) takes several minutes.
#   Run it during a quiet phase.
# - </dev/null per call prevents ebusctl from consuming the loop's stdin.
ADDR=f6
START=${1:-0}
END=${2:-255}
SUBS=${3:-00 01}
r=$START
while [ "$r" -le "$END" ]; do
  hx=$(printf '%02x' "$r")
  for s in $SUBS; do
    printf '0d%s%s = %s\n' "$hx" "$s" \
      "$(ebusctl hex ${ADDR}b509030d${hx}${s} </dev/null 2>&1)"
  done
  r=$((r + 1))
done
