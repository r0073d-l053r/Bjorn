#!/bin/bash
# bjorn_usb_gadget.sh
# Runtime manager for the BJORN USB composite gadget
# Usage:
#   ./bjorn_usb_gadget.sh -u   Bring the gadget up
#   ./bjorn_usb_gadget.sh -d   Bring the gadget down
#   ./bjorn_usb_gadget.sh -r   Reset the gadget (down + up)
#   ./bjorn_usb_gadget.sh -l   Show detailed status
#   ./bjorn_usb_gadget.sh -h   Show help
#
# Notes:
#   This script no longer installs or removes the USB gadget stack.
#   Installation is handled by the BJORN installer.
#   This tool is for runtime diagnostics and recovery only.

set -u

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_VERSION="2.0"
LOG_DIR="/var/log/bjorn_install"
LOG_FILE="$LOG_DIR/bjorn_usb_gadget_$(date +%Y%m%d_%H%M%S).log"

USB_GADGET_SERVICE="usb-gadget.service"
USB_GADGET_SCRIPT="/usr/local/bin/usb-gadget.sh"
DNSMASQ_SERVICE="dnsmasq.service"
DNSMASQ_CONFIG="/etc/dnsmasq.d/usb0"
MODULES_LOAD_FILE="/etc/modules-load.d/usb-gadget.conf"
MODULES_FILE="/etc/modules"
INTERFACES_FILE="/etc/network/interfaces"

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

show_recent_logs() {
    if command -v journalctl >/dev/null 2>&1 && systemctl list-unit-files --type=service | grep -q "^${USB_GADGET_SERVICE}"; then
        log "INFO" "Recent ${USB_GADGET_SERVICE} logs:"
        journalctl -u "$USB_GADGET_SERVICE" -n 20 --no-pager 2>/dev/null || true
    fi
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

usb0_exists() {
    ip link show usb0 >/dev/null 2>&1
}

print_divider() {
    printf '%b%s%b\n' "$CYAN" "============================================================" "$NC"
}

detect_boot_paths() {
    local cmdline=""
    local config=""

    if [ -f /boot/firmware/cmdline.txt ]; then
        cmdline="/boot/firmware/cmdline.txt"
    elif [ -f /boot/cmdline.txt ]; then
        cmdline="/boot/cmdline.txt"
    fi

    if [ -f /boot/firmware/config.txt ]; then
        config="/boot/firmware/config.txt"
    elif [ -f /boot/config.txt ]; then
        config="/boot/config.txt"
    fi

    printf '%s|%s\n' "$cmdline" "$config"
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

show_usage() {
    echo -e "${GREEN}Usage: $0 [OPTIONS]${NC}"
    echo -e "Options:"
    echo -e "  ${BLUE}-u${NC}    Bring USB Gadget up"
    echo -e "  ${BLUE}-d${NC}    Bring USB Gadget down"
    echo -e "  ${BLUE}-r${NC}    Reset USB Gadget (down + up)"
    echo -e "  ${BLUE}-l${NC}    List detailed USB Gadget status"
    echo -e "  ${BLUE}-h${NC}    Show this help message"
    echo -e ""
    echo -e "Examples:"
    echo -e "  $0 -u    Start the BJORN composite gadget"
    echo -e "  $0 -d    Stop the BJORN composite gadget cleanly"
    echo -e "  $0 -r    Reinitialize the gadget if RNDIS/HID is stuck"
    echo -e "  $0 -l    Show services, usb0, /dev/hidg*, and boot config"
    echo -e ""
    echo -e "${YELLOW}This script no longer installs or removes USB Gadget.${NC}"
    echo -e "${YELLOW}That part is handled by the BJORN installer.${NC}"
    if [ "${1:-exit}" = "return" ]; then
        return 0
    fi
    exit 0
}

list_usb_gadget_info() {
    local boot_pair
    local cmdline_file
    local config_file

    boot_pair="$(detect_boot_paths)"
    cmdline_file="${boot_pair%%|*}"
    config_file="${boot_pair##*|}"

    print_divider
    log "SECTION" "BJORN USB Gadget Status"
    print_divider

    log "INFO" "Expected layout: RNDIS usb0 + HID keyboard /dev/hidg0 + HID mouse /dev/hidg1"
    log "INFO" "Script version: ${SCRIPT_VERSION}"
    log "INFO" "Log file: ${LOG_FILE}"

    print_divider
    log "SECTION" "Service Status"
    if service_exists "$USB_GADGET_SERVICE"; then
        service_active "$USB_GADGET_SERVICE" && log "SUCCESS" "${USB_GADGET_SERVICE} is active" || log "WARNING" "${USB_GADGET_SERVICE} is not active"
        service_enabled "$USB_GADGET_SERVICE" && log "SUCCESS" "${USB_GADGET_SERVICE} is enabled at boot" || log "WARNING" "${USB_GADGET_SERVICE} is not enabled at boot"
    else
        log "ERROR" "${USB_GADGET_SERVICE} is not installed on this system"
    fi

    if service_exists "$DNSMASQ_SERVICE"; then
        service_active "$DNSMASQ_SERVICE" && log "SUCCESS" "${DNSMASQ_SERVICE} is active" || log "WARNING" "${DNSMASQ_SERVICE} is not active"
    else
        log "WARNING" "${DNSMASQ_SERVICE} is not installed"
    fi

    print_divider
    log "SECTION" "Runtime Files"
    [ -x "$USB_GADGET_SCRIPT" ] && log "SUCCESS" "${USB_GADGET_SCRIPT} is present and executable" || log "ERROR" "${USB_GADGET_SCRIPT} is missing or not executable"
    [ -c /dev/hidg0 ] && log "SUCCESS" "/dev/hidg0 (keyboard) is available" || log "WARNING" "/dev/hidg0 (keyboard) is not present"
    [ -c /dev/hidg1 ] && log "SUCCESS" "/dev/hidg1 (mouse) is available" || log "WARNING" "/dev/hidg1 (mouse) is not present"

    if ip link show usb0 >/dev/null 2>&1; then
        log "SUCCESS" "usb0 network interface exists"
        ip -brief addr show usb0 2>/dev/null || true
    else
        log "WARNING" "usb0 network interface is missing"
    fi

    if [ -d /sys/kernel/config/usb_gadget/g1 ]; then
        log "SUCCESS" "Composite gadget directory exists: /sys/kernel/config/usb_gadget/g1"
        find /sys/kernel/config/usb_gadget/g1/functions -maxdepth 1 -mindepth 1 -type d 2>/dev/null || true
    else
        log "WARNING" "No active gadget directory found under /sys/kernel/config/usb_gadget/g1"
    fi

    print_divider
    log "SECTION" "Boot Configuration"
    if [ -n "$cmdline_file" ] && [ -f "$cmdline_file" ]; then
        grep -q "modules-load=dwc2" "$cmdline_file" && log "SUCCESS" "dwc2 boot module load is configured in ${cmdline_file}" || log "WARNING" "dwc2 boot module load not found in ${cmdline_file}"
    else
        log "WARNING" "cmdline.txt not found"
    fi

    if [ -n "$config_file" ] && [ -f "$config_file" ]; then
        grep -q "^dtoverlay=dwc2" "$config_file" && log "SUCCESS" "dtoverlay=dwc2 is present in ${config_file}" || log "WARNING" "dtoverlay=dwc2 not found in ${config_file}"
    else
        log "WARNING" "config.txt not found"
    fi

    [ -f "$DNSMASQ_CONFIG" ] && log "SUCCESS" "${DNSMASQ_CONFIG} exists" || log "WARNING" "${DNSMASQ_CONFIG} is missing"
    [ -f "$MODULES_LOAD_FILE" ] && log "INFO" "${MODULES_LOAD_FILE} exists (64-bit style module loading)"
    [ -f "$MODULES_FILE" ] && grep -q "^libcomposite" "$MODULES_FILE" && log "INFO" "libcomposite is referenced in ${MODULES_FILE}"
    [ -f "$INTERFACES_FILE" ] && grep -q "^allow-hotplug usb0" "$INTERFACES_FILE" && log "INFO" "usb0 legacy interface config detected in ${INTERFACES_FILE}"

    print_divider
    log "SECTION" "Quick Recovery Hints"
    log "INFO" "If RNDIS or HID is stuck, run: sudo $0 -r"
    log "INFO" "If startup still fails, inspect logs with: sudo journalctl -u ${USB_GADGET_SERVICE} -f"
    log "INFO" "If HID nodes never appear after installer changes, a reboot may still be required"
}

bring_usb_gadget_down() {
    ensure_root
    print_divider
    log "SECTION" "Bringing USB gadget down"
    print_divider

    if service_exists "$USB_GADGET_SERVICE"; then
        if service_active "$USB_GADGET_SERVICE"; then
            log "INFO" "Stopping ${USB_GADGET_SERVICE}..."
            if systemctl stop "$USB_GADGET_SERVICE"; then
                log "SUCCESS" "Stopped ${USB_GADGET_SERVICE}"
            else
                log "ERROR" "Failed to stop ${USB_GADGET_SERVICE}"
                show_recent_logs
                return 1
            fi
        else
            log "INFO" "${USB_GADGET_SERVICE} is already stopped"
        fi
    else
        log "WARNING" "${USB_GADGET_SERVICE} is not installed, trying direct runtime cleanup"
        if [ -x "$USB_GADGET_SCRIPT" ]; then
            "$USB_GADGET_SCRIPT" stop >> "$LOG_FILE" 2>&1 || true
        fi
    fi

    if [ -x "$USB_GADGET_SCRIPT" ] && [ -d /sys/kernel/config/usb_gadget/g1 ]; then
        log "INFO" "Running direct gadget cleanup via ${USB_GADGET_SCRIPT} stop"
        "$USB_GADGET_SCRIPT" stop >> "$LOG_FILE" 2>&1 || log "WARNING" "Direct cleanup reported a non-fatal issue"
    fi

    if ip link show usb0 >/dev/null 2>&1; then
        log "INFO" "Bringing usb0 interface down"
        ip link set usb0 down >> "$LOG_FILE" 2>&1 || log "WARNING" "usb0 could not be forced down (often harmless)"
    else
        log "INFO" "usb0 is already absent"
    fi

    [ -c /dev/hidg0 ] && log "WARNING" "/dev/hidg0 still exists after stop (may clear on next start/reboot)" || log "SUCCESS" "/dev/hidg0 is no longer exposed"
    [ -c /dev/hidg1 ] && log "WARNING" "/dev/hidg1 still exists after stop (may clear on next start/reboot)" || log "SUCCESS" "/dev/hidg1 is no longer exposed"
    ip link show usb0 >/dev/null 2>&1 && log "WARNING" "usb0 still exists after stop" || log "SUCCESS" "usb0 is no longer present"
}

bring_usb_gadget_up() {
    ensure_root
    print_divider
    log "SECTION" "Bringing USB gadget up"
    print_divider

    if [ ! -x "$USB_GADGET_SCRIPT" ]; then
        log "ERROR" "${USB_GADGET_SCRIPT} is missing. The gadget runtime is not installed."
        return 1
    fi

    if service_exists "$USB_GADGET_SERVICE"; then
        log "INFO" "Reloading systemd daemon"
        systemctl daemon-reload >> "$LOG_FILE" 2>&1 || log "WARNING" "systemd daemon-reload reported an issue"

        log "INFO" "Starting ${USB_GADGET_SERVICE}..."
        if systemctl start "$USB_GADGET_SERVICE"; then
            log "SUCCESS" "Start command sent to ${USB_GADGET_SERVICE}"
        else
            log "ERROR" "Failed to start ${USB_GADGET_SERVICE}"
            show_recent_logs
            return 1
        fi
    else
        log "WARNING" "${USB_GADGET_SERVICE} is not installed, running ${USB_GADGET_SCRIPT} directly"
        if "$USB_GADGET_SCRIPT" >> "$LOG_FILE" 2>&1; then
            log "SUCCESS" "Runtime script executed directly"
        else
            log "ERROR" "Runtime script failed"
            return 1
        fi
    fi

    wait_for_condition "${USB_GADGET_SERVICE} to become active" 10 service_active "$USB_GADGET_SERVICE" || true
    wait_for_condition "usb0 to appear" 12 usb0_exists || true

    if service_exists "$DNSMASQ_SERVICE"; then
        log "INFO" "Restarting ${DNSMASQ_SERVICE} to refresh DHCP on usb0"
        systemctl restart "$DNSMASQ_SERVICE" >> "$LOG_FILE" 2>&1 || log "WARNING" "Failed to restart ${DNSMASQ_SERVICE}"
    fi

    [ -c /dev/hidg0 ] && log "SUCCESS" "/dev/hidg0 (keyboard) is ready" || log "WARNING" "/dev/hidg0 not present yet"
    [ -c /dev/hidg1 ] && log "SUCCESS" "/dev/hidg1 (mouse) is ready" || log "WARNING" "/dev/hidg1 not present yet"

    if ip link show usb0 >/dev/null 2>&1; then
        log "SUCCESS" "usb0 is present"
        ip -brief addr show usb0 2>/dev/null || true
    else
        log "WARNING" "usb0 is still missing after startup"
    fi

    log "INFO" "If HID is still missing after a clean start, a reboot can still be required depending on the board/kernel state"
}

reset_usb_gadget() {
    ensure_root
    print_divider
    log "SECTION" "Resetting USB gadget (down + up)"
    print_divider

    bring_usb_gadget_down || log "WARNING" "Down phase reported an issue, continuing with recovery"
    log "INFO" "Waiting 2 seconds before bringing the gadget back up"
    sleep 2
    bring_usb_gadget_up
}

display_main_menu() {
    while true; do
        clear
        print_divider
        echo -e "${CYAN} BJORN USB Gadget Runtime Manager v${SCRIPT_VERSION}${NC}"
        print_divider
        echo -e "${BLUE} 1.${NC} Bring USB Gadget up"
        echo -e "${BLUE} 2.${NC} Bring USB Gadget down"
        echo -e "${BLUE} 3.${NC} Reset USB Gadget (down + up)"
        echo -e "${BLUE} 4.${NC} List detailed USB Gadget status"
        echo -e "${BLUE} 5.${NC} Show help"
        echo -e "${BLUE} 6.${NC} Exit"
        echo -e ""
        echo -e "${YELLOW}Note:${NC} installation/removal is no longer handled here."
        echo -n -e "${GREEN}Choose an option (1-6): ${NC}"
        read -r choice

        case "$choice" in
            1)
                bring_usb_gadget_up
                echo ""
                read -r -p "Press Enter to return to the menu..."
                ;;
            2)
                bring_usb_gadget_down
                echo ""
                read -r -p "Press Enter to return to the menu..."
                ;;
            3)
                reset_usb_gadget
                echo ""
                read -r -p "Press Enter to return to the menu..."
                ;;
            4)
                list_usb_gadget_info
                echo ""
                read -r -p "Press Enter to return to the menu..."
                ;;
            5)
                show_usage return
                echo ""
                read -r -p "Press Enter to return to the menu..."
                ;;
            6)
                log "INFO" "Exiting BJORN USB Gadget Runtime Manager"
                exit 0
                ;;
            *)
                log "ERROR" "Invalid option. Please choose between 1 and 6."
                sleep 2
                ;;
        esac
    done
}

while getopts ":udrlhf" opt; do
    case "$opt" in
        u)
            bring_usb_gadget_up
            exit $?
            ;;
        d)
            bring_usb_gadget_down
            exit $?
            ;;
        r)
            reset_usb_gadget
            exit $?
            ;;
        l)
            list_usb_gadget_info
            exit 0
            ;;
        h)
            show_usage
            ;;
        f)
            log "ERROR" "Option -f (install) has been removed. Use -u to bring the gadget up or -r to reset it."
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
