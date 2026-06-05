#!/bin/sh
# Bring all cores online, read max frequencies, then restore original state

for cpu_dir in /sys/devices/system/cpu/cpu[0-9]*; do
    cpu_id=$(basename "$cpu_dir")
    online_path="$cpu_dir/online"
    freq_path="$cpu_dir/cpufreq/cpuinfo_max_freq"

    # Save original online state (cpu0 has no online file, always on)
    if [ -f "$online_path" ]; then
        orig=$(cat "$online_path")
        # Bring online if offline
        if [ "$orig" = "0" ]; then
            echo 1 > "$online_path"
            sleep 0.1
        fi
    else
        orig="1"
    fi

    # Read frequency
    if [ -f "$freq_path" ]; then
        freq=$(cat "$freq_path")
        echo "$cpu_id: ${freq} kHz"
    else
        echo "$cpu_id: no cpufreq"
    fi

    # Restore original state
    if [ -f "$online_path" ] && [ "$orig" = "0" ]; then
        echo 0 > "$online_path"
    fi
done
