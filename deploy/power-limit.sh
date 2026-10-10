#!/bin/sh
# Cap the CPU's turbo bursts. The firmware lets this 15 W i3-1315U draw 51-55 W for about 30
# seconds (PL2); a prompt-heavy model request uses all of that and took the CPU from 38°C to
# 88°C in 28 seconds, and to 95°C on 2026-10-03 from a warmer start. Measured again on 2026-10-04,
# 25 W bursts still peaked at 88°C, so this trims the bursts but the heat guard in server.py
# is what actually protects the machine. The firmware can restore its own limits (after a resume or a
# charger change), so battery-limit's timer pattern runs this every minute; it only writes
# when a value differs. As root (the powercap files are root-only).
BURST_W=${POWER_BURST_W:-25}
want=$((BURST_W * 1000000))
for zone in /sys/class/powercap/intel-rapl:0 /sys/class/powercap/intel-rapl-mmio:0; do
    [ -d "$zone" ] || continue
    for c in 0 1 2; do
        [ "$(cat "$zone/constraint_${c}_name" 2>/dev/null)" = short_term ] || continue
        f="$zone/constraint_${c}_power_limit_uw"
        have=$(cat "$f" 2>/dev/null) || continue
        if [ "$have" != "$want" ]; then
            echo "$want" > "$f" && echo "$(basename "$zone"): burst power $((have / 1000000)) W -> $BURST_W W"
        fi
    done
done
