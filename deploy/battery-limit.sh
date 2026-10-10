#!/bin/sh
# Keep the laptop battery between 75% and 80% while it's on the charger, for battery
# health. The Lenovo firmware has no charge threshold, only "Long_Life" (conservation
# mode, which stops charging and runs the laptop from the charger). So: switch it on at
# 80% or above, off at 75% or below; in between, leave it as it is. Run every minute by
# battery-limit.timer, as root (the kernel's switch is root-only).
# "battery-limit hold" turns Long_Life on whatever the charge, run at shutdown by
# battery-limit-hold.service: the firmware keeps the mode while the laptop is off, and on
# 2026-10-08 the battery ran flat with it off, so the switched-off laptop charged to 100%.
# With it on, a switched-off laptop charges only to the firmware's conservation level
# (about 60%), and the timer takes it to 80% after boot.
STOP=${BATTERY_STOP:-80}
START=${BATTERY_START:-75}
BAT=${BATTERY_DIR:-/sys/class/power_supply/BAT0}
CONS=${CONSERVATION_FILE:-/sys/bus/platform/drivers/ideapad_acpi/VPC2004:00/conservation_mode}

cap=$(cat "$BAT/capacity" 2>/dev/null) || exit 0
case "$cap" in ''|*[!0-9]*) exit 0 ;; esac

if [ -w "$BAT/charge_types" ]; then
    # newer kernels: [Fast] Standard Long_Life, the current one in brackets
    case "$(cat "$BAT/charge_types")" in *"[Long_Life]"*) on=1 ;; *) on=0 ;; esac
    set_mode() { if [ "$1" = 1 ]; then echo Long_Life > "$BAT/charge_types"; else echo Standard > "$BAT/charge_types"; fi; }
elif [ -w "$CONS" ]; then
    on=$(cat "$CONS")
    set_mode() { echo "$1" > "$CONS"; }
else
    exit 0  # no charge control on this machine
fi

if [ "$1" = hold ]; then
    [ "$on" = 1 ] || { set_mode 1 && echo "shutting down at $cap%: Long_Life on, so it won't charge to 100% while off"; }
elif [ "$cap" -ge "$STOP" ] && [ "$on" != 1 ]; then
    set_mode 1 && echo "battery at $cap%: charging paused (Long_Life on)"
elif [ "$cap" -le "$START" ] && [ "$on" = 1 ]; then
    set_mode 0 && echo "battery at $cap%: charging resumed (Long_Life off)"
fi
