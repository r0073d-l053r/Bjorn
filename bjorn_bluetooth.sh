#!/bin/bash
# bjorn_bluetooth.sh
# Runtime manager for the BJORN Bluetooth PAN stack
# Usage:
#   ./bjorn_bluetooth.sh -u   Bring Bluetooth PAN services up
#   ./bjorn_bluetooth.sh -d   Bring Bluetooth PAN services down
#   ./bjorn_bluetooth.sh -r   Reset Bluetooth PAN services
#   ./bjorn_bluetooth.sh -l   Show detailed Bluetooth status
#   ./bjorn_bluetooth.sh -s   Scan nearby Bluetooth devices
#   ./bjorn_bluetooth.sh -p   Launch pairing assistant
#   ./bjorn_bluetooth.sh -c   Connect now to configured target
#   ./bjorn_bluetooth.sh -t   Trust a known device
#   ./bjorn_bluetooth.sh -x   Disconnect current PAN session
#   ./bjorn_bluetooth.sh -f   Forget/remove a known device
#   ./bjorn_bluetooth.sh -h   Show help
#
# Notes:
#   This script no longer installs or removes Bluetooth PAN.
#   Installation is handled by the BJORN installer.
#   This tool is for runtime diagnostics, pairing, trust, connect, and recovery.

set -u

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_VERSION="2.0"
BJORN_USER="bjorn"
BT_SETTINGS_DIR="/home/${BJORN_USER}/.settings_bjorn"
BT_CONFIG="${BT_SETTINGS_DIR}/bt.json"
AUTO_BT_SCRIPT="/usr/local/bin/auto_bt_connect.py"
AUTO_BT_SERVICE="auto_bt_connect.service"
BLUETOOTH_SERVICE="bluetooth.service"
LOG_DIR="/var/log/bjorn_install"
LOG_FILE="$LOG_DIR/bjorn_bluetooth_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$LOG_DIR" 2>/dev/null || true
touch "$LOG_FILE" 2>/dev/null || true

log() {
    local level="$1"
    shift
    local message="[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $*"
    local color="$NC"

    case "$level" in
        ERROR) color="$RED" ;;
        SUCCESS) color="$GREEN" ;;
        WARNING) color="$YELLOW" ;;
        INFO) color="$BLUE" ;;
        SECTION) color="$CYAN" ;;
    esac

    printf '%s\n' "$message" >> "$LOG_FILE" 2>/dev/null || true
    printf '%b%s%b\n' "$color" "$message" "$NC"
}

print_divider() {
    printf '%b%s%b\n' "$CYAN" "============================================================" "$NC"
}

ensure_root() {
    if [ "$(id -u)" -ne 0 ]; then
        log "ERROR" "This command must be run as root. Please use sudo."
        exit 1
    fi
}

service_exists() {
    systemctl list-unit-files --type=service 2>/dev/null | grep -q "^$1"
}

service_active() {
    systemctl is-active --quiet "$1"
}

service_enabled() {
    systemctl is-enabled --quiet "$1"
}

bnep0_exists() {
    ip link show bnep0 >/dev/null 2>&1
}

wait_for_condition() {
    local description="$1"
    local attempts="$2"
    shift 2

    local i=1
    while [ "$i" -le "$attempts" ]; do
        if "$@"; then
            log "SUCCESS" "$description"
            return 0
        fi
        log "INFO" "Waiting for $description ($i/$attempts)..."
        sleep 1
        i=$((i + 1))
    done

    log "WARNING" "$description not reached after ${attempts}s"
    return 1
}

show_recent_logs() {
    if command -v journalctl >/dev/null 2>&1; then
        if service_exists "$AUTO_BT_SERVICE"; then
            log "INFO" "Recent ${AUTO_BT_SERVICE} logs:"
            journalctl -u "$AUTO_BT_SERVICE" -n 20 --no-pager 2>/dev/null || true
        fi
        if service_exists "$BLUETOOTH_SERVICE"; then
            log "INFO" "Recent ${BLUETOOTH_SERVICE} logs:"
            journalctl -u "$BLUETOOTH_SERVICE" -n 10 --no-pager 2>/dev/null || true
        fi
    fi
}

run_btctl() {
    local output
    output="$(printf '%s\n' "$@" "quit" | bluetoothctl 2>&1)"
    printf '%s\n' "$output" >> "$LOG_FILE" 2>/dev/null || true
    printf '%s\n' "$output"
}

bluetooth_power_on() {
    ensure_root
    if ! service_active "$BLUETOOTH_SERVICE"; then
        log "INFO" "Starting ${BLUETOOTH_SERVICE}..."
        systemctl start "$BLUETOOTH_SERVICE" >> "$LOG_FILE" 2>&1 || {
            log "ERROR" "Failed to start ${BLUETOOTH_SERVICE}"
            return 1
        }
    fi

    run_btctl "power on" >/dev/null
    run_btctl "agent on" >/dev/null
    run_btctl "default-agent" >/dev/null
    return 0
}

ensure_bt_settings_dir() {
    mkdir -p "$BT_SETTINGS_DIR" >> "$LOG_FILE" 2>&1 || return 1
    chown "$BJORN_USER:$BJORN_USER" "$BT_SETTINGS_DIR" >> "$LOG_FILE" 2>&1 || true
}

get_configured_mac() {
    if [ ! -f "$BT_CONFIG" ]; then
        return 1
    fi

    sed -n 's/.*"device_mac"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$BT_CONFIG" | head -n1
}

write_configured_mac() {
    local mac="$1"

    ensure_bt_settings_dir || {
        log "ERROR" "Failed to create ${BT_SETTINGS_DIR}"
        return 1
    }

    cat > "$BT_CONFIG" <<EOF
{
    "device_mac": "$mac"
}
EOF

    chown "$BJORN_USER:$BJORN_USER" "$BT_CONFIG" >> "$LOG_FILE" 2>&1 || true
    chmod 644 "$BT_CONFIG" >> "$LOG_FILE" 2>&1 || true
    log "SUCCESS" "Updated auto-connect target in ${BT_CONFIG}: ${mac:-<empty>}"
    return 0
}

device_info() {
    local mac="$1"
    bluetoothctl info "$mac" 2>/dev/null
}

device_flag() {
    local mac="$1"
    local key="$2"
    device_info "$mac" | sed -n "s/^[[:space:]]*${key}:[[:space:]]*//p" | head -n1
}

device_name() {
    local mac="$1"
    local name
    name="$(device_info "$mac" | sed -n 's/^[[:space:]]*Name:[[:space:]]*//p' | head -n1)"
    if [ -z "$name" ]; then
        name="$(bluetoothctl devices 2>/dev/null | sed -n "s/^Device ${mac} //p" | head -n1)"
    fi
    printf '%s\n' "${name:-Unknown device}"
}

load_devices() {
    local mode="${1:-all}"
    local source_cmd="devices"
    local line mac name

    DEVICE_MACS=()
    DEVICE_NAMES=()

    if [ "$mode" = "paired" ]; then
        source_cmd="paired-devices"
    fi

    while IFS= read -r line; do
        mac="$(printf '%s\n' "$line" | sed -n 's/^Device \([0-9A-F:]\{17\}\) .*/\1/p')"
        name="$(printf '%s\n' "$line" | sed -n 's/^Device [0-9A-F:]\{17\} \(.*\)$/\1/p')"
        if [ -n "$mac" ]; then
            DEVICE_MACS+=("$mac")
            DEVICE_NAMES+=("${name:-Unknown device}")
        fi
    done < <(bluetoothctl "$source_cmd" 2>/dev/null)
}

print_device_list() {
    local configured_mac="${1:-}"
    local i status paired trusted connected

    if [ "${#DEVICE_MACS[@]}" -eq 0 ]; then
        log "WARNING" "No devices found"
        return 1
    fi

    for ((i=0; i<${#DEVICE_MACS[@]}; i++)); do
        paired="$(device_flag "${DEVICE_MACS[$i]}" "Paired")"
        trusted="$(device_flag "${DEVICE_MACS[$i]}" "Trusted")"
        connected="$(device_flag "${DEVICE_MACS[$i]}" "Connected")"
        status=""
        [ "$paired" = "yes" ] && status="${status} paired"
        [ "$trusted" = "yes" ] && status="${status} trusted"
        [ "$connected" = "yes" ] && status="${status} connected"
        [ "${DEVICE_MACS[$i]}" = "$configured_mac" ] && status="${status} configured"
        printf '%b[%d]%b %s  %s%b%s%b\n' "$BLUE" "$((i + 1))" "$NC" "${DEVICE_MACS[$i]}" "${DEVICE_NAMES[$i]}" "$YELLOW" "${status:- new}" "$NC"
    done
    return 0
}

select_device() {
    local mode="${1:-all}"
    local configured_mac choice index

    configured_mac="$(get_configured_mac 2>/dev/null || true)"
    load_devices "$mode"

    if [ "${#DEVICE_MACS[@]}" -eq 0 ]; then
        if [ "$mode" = "all" ]; then
            log "WARNING" "No known devices yet. Run a scan first."
        else
            log "WARNING" "No paired devices found."
        fi
        return 1
    fi

    print_divider
    log "SECTION" "Select a Bluetooth device"
    print_device_list "$configured_mac" || return 1
    echo -n -e "${GREEN}Choose a device number (or 0 to cancel): ${NC}"
    read -r choice

    if [ -z "$choice" ] || [ "$choice" = "0" ]; then
        log "INFO" "Selection cancelled"
        return 1
    fi

    if ! [[ "$choice" =~ ^[0-9]+$ ]]; then
        log "ERROR" "Invalid selection"
        return 1
    fi

    index=$((choice - 1))
    if [ "$index" -lt 0 ] || [ "$index" -ge "${#DEVICE_MACS[@]}" ]; then
        log "ERROR" "Selection out of range"
        return 1
    fi

    SELECTED_DEVICE_MAC="${DEVICE_MACS[$index]}"
    SELECTED_DEVICE_NAME="${DEVICE_NAMES[$index]}"
    log "INFO" "Selected ${SELECTED_DEVICE_NAME} (${SELECTED_DEVICE_MAC})"
    return 0
}

scan_bluetooth_devices() {
    ensure_root
    local duration="${1:-12}"

    print_divider
    log "SECTION" "Scanning nearby Bluetooth devices"
    print_divider

    bluetooth_power_on || return 1
    log "INFO" "Scanning for ${duration} seconds..."
    timeout "${duration}s" bluetoothctl scan on >> "$LOG_FILE" 2>&1 || true
    run_btctl "scan off" >/dev/null
    log "SUCCESS" "Scan complete"
    load_devices all
    print_device_list "$(get_configured_mac 2>/dev/null || true)" || true
}

pair_device() {
    local mac="$1"
    local output

    bluetooth_power_on || return 1
    log "INFO" "Pairing with ${mac}..."
    output="$(run_btctl "pair ${mac}")"
    if printf '%s\n' "$output" | grep -qi "Pairing successful"; then
        log "SUCCESS" "Pairing successful for ${mac}"
        return 0
    fi

    if [ "$(device_flag "$mac" "Paired")" = "yes" ]; then
        log "INFO" "Device ${mac} is already paired"
        return 0
    fi

    log "ERROR" "Pairing failed for ${mac}"
    printf '%s\n' "$output"
    return 1
}

trust_device() {
    local mac="$1"
    local output

    bluetooth_power_on || return 1
    log "INFO" "Trusting ${mac}..."
    output="$(run_btctl "trust ${mac}")"
    if printf '%s\n' "$output" | grep -qi "trust succeeded"; then
        log "SUCCESS" "Trust succeeded for ${mac}"
        return 0
    fi

    if [ "$(device_flag "$mac" "Trusted")" = "yes" ]; then
        log "INFO" "Device ${mac} is already trusted"
        return 0
    fi

    log "ERROR" "Trust failed for ${mac}"
    printf '%s\n' "$output"
    return 1
}

disconnect_pan_session() {
    ensure_root
    local configured_mac="${1:-}"

    print_divider
    log "SECTION" "Disconnecting Bluetooth PAN"
    print_divider

    if service_exists "$AUTO_BT_SERVICE" && service_active "$AUTO_BT_SERVICE"; then
        log "INFO" "Stopping ${AUTO_BT_SERVICE} to prevent immediate reconnect"
        systemctl stop "$AUTO_BT_SERVICE" >> "$LOG_FILE" 2>&1 || log "WARNING" "Failed to stop ${AUTO_BT_SERVICE}"
    fi

    if bnep0_exists; then
        log "INFO" "Releasing DHCP lease on bnep0"
        dhclient -r bnep0 >> "$LOG_FILE" 2>&1 || true
        ip link set bnep0 down >> "$LOG_FILE" 2>&1 || true
    else
        log "INFO" "bnep0 is not present"
    fi

    pkill -f "bt-network -c" >> "$LOG_FILE" 2>&1 || true
    pkill -f "bt-network" >> "$LOG_FILE" 2>&1 || true

    if [ -n "$configured_mac" ]; then
        log "INFO" "Requesting Bluetooth disconnect for ${configured_mac}"
        run_btctl "disconnect ${configured_mac}" >/dev/null || true
    fi

    bnep0_exists && log "WARNING" "bnep0 still exists after disconnect" || log "SUCCESS" "Bluetooth PAN session is down"
}

connect_to_target_now() {
    ensure_root
    local mac="$1"
    local previous_mac

    if [ -z "$mac" ]; then
        log "ERROR" "No target MAC specified"
        return 1
    fi

    print_divider
    log "SECTION" "Connecting Bluetooth PAN now"
    print_divider

    bluetooth_power_on || return 1

    if [ "$(device_flag "$mac" "Paired")" != "yes" ]; then
        log "WARNING" "Target ${mac} is not paired yet"
    fi
    if [ "$(device_flag "$mac" "Trusted")" != "yes" ]; then
        log "WARNING" "Target ${mac} is not trusted yet"
    fi

    previous_mac="$(get_configured_mac 2>/dev/null || true)"
    write_configured_mac "$mac" || return 1
    disconnect_pan_session "$previous_mac" || true

    if service_exists "$AUTO_BT_SERVICE"; then
        log "INFO" "Restarting ${AUTO_BT_SERVICE}"
        systemctl daemon-reload >> "$LOG_FILE" 2>&1 || true
        systemctl restart "$AUTO_BT_SERVICE" >> "$LOG_FILE" 2>&1 || {
            log "ERROR" "Failed to restart ${AUTO_BT_SERVICE}"
            show_recent_logs
            return 1
        }
    else
        log "ERROR" "${AUTO_BT_SERVICE} is not installed"
        return 1
    fi

    wait_for_condition "${AUTO_BT_SERVICE} to become active" 10 service_active "$AUTO_BT_SERVICE" || true
    wait_for_condition "bnep0 to appear" 15 bnep0_exists || true

    if bnep0_exists; then
        log "SUCCESS" "Bluetooth PAN link is up on bnep0"
        ip -brief addr show bnep0 2>/dev/null || true
    else
        log "WARNING" "bnep0 is still missing. Pairing/trust may be OK but PAN did not come up yet."
        show_recent_logs
    fi
}

set_auto_connect_target() {
    ensure_root

    if ! select_device all; then
        return 1
    fi

    write_configured_mac "$SELECTED_DEVICE_MAC"
}

pairing_assistant() {
    ensure_root

    print_divider
    log "SECTION" "Bluetooth pairing assistant"
    print_divider

    scan_bluetooth_devices 12 || true
    if ! select_device all; then
        return 1
    fi

    pair_device "$SELECTED_DEVICE_MAC" || return 1
    trust_device "$SELECTED_DEVICE_MAC" || return 1
    write_configured_mac "$SELECTED_DEVICE_MAC" || return 1

    echo -n -e "${GREEN}Connect to this device now for PAN? [Y/n]: ${NC}"
    read -r answer
    case "${answer:-Y}" in
        n|N)
            log "INFO" "Pairing assistant completed without immediate PAN connect"
            ;;
        *)
            connect_to_target_now "$SELECTED_DEVICE_MAC"
            ;;
    esac
}

forget_device() {
    ensure_root
    local configured_mac output

    configured_mac="$(get_configured_mac 2>/dev/null || true)"
    if ! select_device all; then
        return 1
    fi

    if [ "$SELECTED_DEVICE_MAC" = "$configured_mac" ]; then
        log "WARNING" "This device is currently configured as the auto-connect target"
        disconnect_pan_session "$SELECTED_DEVICE_MAC" || true
        write_configured_mac ""
    fi

    log "INFO" "Removing ${SELECTED_DEVICE_NAME} (${SELECTED_DEVICE_MAC}) from BlueZ"
    output="$(run_btctl "remove ${SELECTED_DEVICE_MAC}")"
    if printf '%s\n' "$output" | grep -qi "Device has been removed"; then
        log "SUCCESS" "Device removed"
        return 0
    fi

    if ! bluetoothctl devices 2>/dev/null | grep -q "$SELECTED_DEVICE_MAC"; then
        log "SUCCESS" "Device no longer appears in known devices"
        return 0
    fi

    log "ERROR" "Failed to remove device"
    printf '%s\n' "$output"
    return 1
}

trust_selected_device() {
    ensure_root
    if ! select_device all; then
        return 1
    fi
    trust_device "$SELECTED_DEVICE_MAC"
}

list_bluetooth_status() {
    local configured_mac controller_info paired trusted connected

    print_divider
    log "SECTION" "BJORN Bluetooth PAN Status"
    print_divider

    controller_info="$(run_btctl "show")"
    configured_mac="$(get_configured_mac 2>/dev/null || true)"

    if service_exists "$BLUETOOTH_SERVICE"; then
        service_active "$BLUETOOTH_SERVICE" && log "SUCCESS" "${BLUETOOTH_SERVICE} is active" || log "WARNING" "${BLUETOOTH_SERVICE} is not active"
        service_enabled "$BLUETOOTH_SERVICE" && log "SUCCESS" "${BLUETOOTH_SERVICE} is enabled at boot" || log "WARNING" "${BLUETOOTH_SERVICE} is not enabled at boot"
    else
        log "ERROR" "${BLUETOOTH_SERVICE} is not installed"
    fi

    if service_exists "$AUTO_BT_SERVICE"; then
        service_active "$AUTO_BT_SERVICE" && log "SUCCESS" "${AUTO_BT_SERVICE} is active" || log "WARNING" "${AUTO_BT_SERVICE} is not active"
        service_enabled "$AUTO_BT_SERVICE" && log "SUCCESS" "${AUTO_BT_SERVICE} is enabled at boot" || log "WARNING" "${AUTO_BT_SERVICE} is not enabled at boot"
    else
        log "ERROR" "${AUTO_BT_SERVICE} is not installed"
    fi

    [ -f "$AUTO_BT_SCRIPT" ] && log "SUCCESS" "${AUTO_BT_SCRIPT} exists" || log "ERROR" "${AUTO_BT_SCRIPT} is missing"
    [ -f "$BT_CONFIG" ] && log "SUCCESS" "${BT_CONFIG} exists" || log "WARNING" "${BT_CONFIG} is missing"

    if printf '%s\n' "$controller_info" | grep -q "Powered: yes"; then
        log "SUCCESS" "Bluetooth controller is powered on"
    else
        log "WARNING" "Bluetooth controller is not powered on"
    fi

    if [ -n "$configured_mac" ]; then
        log "INFO" "Configured auto-connect target: ${configured_mac} ($(device_name "$configured_mac"))"
        paired="$(device_flag "$configured_mac" "Paired")"
        trusted="$(device_flag "$configured_mac" "Trusted")"
        connected="$(device_flag "$configured_mac" "Connected")"
        log "INFO" "Configured target state: paired=${paired:-unknown}, trusted=${trusted:-unknown}, connected=${connected:-unknown}"
    else
        log "WARNING" "No auto-connect target configured in ${BT_CONFIG}"
    fi

    if bnep0_exists; then
        log "SUCCESS" "bnep0 interface exists"
        ip -brief addr show bnep0 2>/dev/null || true
    else
        log "WARNING" "bnep0 interface is not present"
    fi

    print_divider
    log "SECTION" "Known Devices"
    load_devices all
    print_device_list "$configured_mac" || true

    print_divider
    log "SECTION" "Quick Recovery Hints"
    log "INFO" "Use -p for the pairing assistant"
    log "INFO" "Use -c to connect now to the configured target"
    log "INFO" "Use -r to reset Bluetooth PAN if bnep0 is stuck"
    log "INFO" "Follow logs with: sudo journalctl -u ${AUTO_BT_SERVICE} -f"
}

bring_bluetooth_pan_up() {
    ensure_root
    local configured_mac

    print_divider
    log "SECTION" "Bringing Bluetooth PAN up"
    print_divider

    bluetooth_power_on || return 1
    configured_mac="$(get_configured_mac 2>/dev/null || true)"

    if [ -z "$configured_mac" ]; then
        log "WARNING" "No configured target in ${BT_CONFIG}"
        log "INFO" "Use the pairing assistant (-p) or set a target from the menu"
    fi

    if service_exists "$AUTO_BT_SERVICE"; then
        systemctl daemon-reload >> "$LOG_FILE" 2>&1 || true
        systemctl start "$AUTO_BT_SERVICE" >> "$LOG_FILE" 2>&1 || {
            log "ERROR" "Failed to start ${AUTO_BT_SERVICE}"
            show_recent_logs
            return 1
        }
        log "SUCCESS" "Start command sent to ${AUTO_BT_SERVICE}"
    else
        log "ERROR" "${AUTO_BT_SERVICE} is not installed"
        return 1
    fi

    wait_for_condition "${AUTO_BT_SERVICE} to become active" 10 service_active "$AUTO_BT_SERVICE" || true
    if [ -n "$configured_mac" ]; then
        wait_for_condition "bnep0 to appear" 15 bnep0_exists || true
    fi

    if bnep0_exists; then
        log "SUCCESS" "Bluetooth PAN is up on bnep0"
        ip -brief addr show bnep0 2>/dev/null || true
    else
        log "WARNING" "Bluetooth PAN is not up yet"
    fi
}

bring_bluetooth_pan_down() {
    ensure_root
    local configured_mac

    print_divider
    log "SECTION" "Bringing Bluetooth PAN down"
    print_divider

    configured_mac="$(get_configured_mac 2>/dev/null || true)"
    disconnect_pan_session "$configured_mac"
}

reset_bluetooth_pan() {
    ensure_root

    print_divider
    log "SECTION" "Resetting Bluetooth PAN"
    print_divider

    bring_bluetooth_pan_down || log "WARNING" "Down phase reported an issue, continuing"
    log "INFO" "Waiting 2 seconds before restart"
    sleep 2
    bring_bluetooth_pan_up
}

show_usage() {
    echo -e "${GREEN}Usage: $0 [OPTIONS]${NC}"
    echo -e "Options:"
    echo -e "  ${BLUE}-u${NC}    Bring Bluetooth PAN services up"
    echo -e "  ${BLUE}-d${NC}    Bring Bluetooth PAN services down"
    echo -e "  ${BLUE}-r${NC}    Reset Bluetooth PAN services"
    echo -e "  ${BLUE}-l${NC}    Show detailed Bluetooth status"
    echo -e "  ${BLUE}-s${NC}    Scan nearby Bluetooth devices"
    echo -e "  ${BLUE}-p${NC}    Launch pairing assistant"
    echo -e "  ${BLUE}-c${NC}    Connect now to configured target"
    echo -e "  ${BLUE}-t${NC}    Trust a known device"
    echo -e "  ${BLUE}-x${NC}    Disconnect current PAN session"
    echo -e "  ${BLUE}-f${NC}    Forget/remove a known device"
    echo -e "  ${BLUE}-h${NC}    Show this help message"
    echo -e ""
    echo -e "Examples:"
    echo -e "  $0 -p    Scan, pair, trust, set target, and optionally connect now"
    echo -e "  $0 -u    Start Bluetooth and the auto PAN reconnect service"
    echo -e "  $0 -r    Reset a stuck bnep0/PAN session"
    echo -e "  $0 -f    Forget a previously paired device"
    echo -e ""
    echo -e "${YELLOW}This script no longer installs or removes Bluetooth PAN.${NC}"
    echo -e "${YELLOW}That part is handled by the BJORN installer.${NC}"
    if [ "${1:-exit}" = "return" ]; then
        return 0
    fi
    exit 0
}

display_main_menu() {
    while true; do
        clear
        print_divider
        echo -e "${CYAN} BJORN Bluetooth Runtime Manager v${SCRIPT_VERSION}${NC}"
        print_divider
        echo -e "${BLUE} 1.${NC} Show Bluetooth PAN status"
        echo -e "${BLUE} 2.${NC} Bring Bluetooth PAN up"
        echo -e "${BLUE} 3.${NC} Bring Bluetooth PAN down"
        echo -e "${BLUE} 4.${NC} Reset Bluetooth PAN"
        echo -e "${BLUE} 5.${NC} Scan nearby Bluetooth devices"
        echo -e "${BLUE} 6.${NC} Pairing assistant"
        echo -e "${BLUE} 7.${NC} Connect now to configured target"
        echo -e "${BLUE} 8.${NC} Set/change auto-connect target"
        echo -e "${BLUE} 9.${NC} Trust a known device"
        echo -e "${BLUE}10.${NC} Disconnect current PAN session"
        echo -e "${BLUE}11.${NC} Forget/remove a known device"
        echo -e "${BLUE}12.${NC} Show help"
        echo -e "${BLUE}13.${NC} Exit"
        echo -e ""
        echo -e "${YELLOW}Note:${NC} installation/removal is no longer handled here."
        echo -n -e "${GREEN}Choose an option (1-13): ${NC}"
        read -r choice

        case "$choice" in
            1)
                list_bluetooth_status
                echo ""
                read -r -p "Press Enter to return to the menu..."
                ;;
            2)
                bring_bluetooth_pan_up
                echo ""
                read -r -p "Press Enter to return to the menu..."
                ;;
            3)
                bring_bluetooth_pan_down
                echo ""
                read -r -p "Press Enter to return to the menu..."
                ;;
            4)
                reset_bluetooth_pan
                echo ""
                read -r -p "Press Enter to return to the menu..."
                ;;
            5)
                scan_bluetooth_devices 12
                echo ""
                read -r -p "Press Enter to return to the menu..."
                ;;
            6)
                pairing_assistant
                echo ""
                read -r -p "Press Enter to return to the menu..."
                ;;
            7)
                connect_to_target_now "$(get_configured_mac 2>/dev/null || true)"
                echo ""
                read -r -p "Press Enter to return to the menu..."
                ;;
            8)
                set_auto_connect_target
                echo ""
                read -r -p "Press Enter to return to the menu..."
                ;;
            9)
                trust_selected_device
                echo ""
                read -r -p "Press Enter to return to the menu..."
                ;;
            10)
                disconnect_pan_session "$(get_configured_mac 2>/dev/null || true)"
                echo ""
                read -r -p "Press Enter to return to the menu..."
                ;;
            11)
                forget_device
                echo ""
                read -r -p "Press Enter to return to the menu..."
                ;;
            12)
                show_usage return
                echo ""
                read -r -p "Press Enter to return to the menu..."
                ;;
            13)
                log "INFO" "Exiting BJORN Bluetooth Runtime Manager"
                exit 0
                ;;
            *)
                log "ERROR" "Invalid option. Please choose between 1 and 13."
                sleep 2
                ;;
        esac
    done
}

while getopts ":udrlspctxfh" opt; do
    case "$opt" in
        u)
            bring_bluetooth_pan_up
            exit $?
            ;;
        d)
            bring_bluetooth_pan_down
            exit $?
            ;;
        r)
            reset_bluetooth_pan
            exit $?
            ;;
        l)
            list_bluetooth_status
            exit 0
            ;;
        s)
            scan_bluetooth_devices 12
            exit $?
            ;;
        p)
            pairing_assistant
            exit $?
            ;;
        c)
            connect_to_target_now "$(get_configured_mac 2>/dev/null || true)"
            exit $?
            ;;
        t)
            trust_selected_device
            exit $?
            ;;
        x)
            disconnect_pan_session "$(get_configured_mac 2>/dev/null || true)"
            exit $?
            ;;
        f)
            forget_device
            exit $?
            ;;
        h)
            show_usage
            ;;
        \?)
            log "ERROR" "Invalid option: -$OPTARG"
            show_usage
            ;;
    esac
done

if [ $OPTIND -eq 1 ]; then
    display_main_menu
fi
