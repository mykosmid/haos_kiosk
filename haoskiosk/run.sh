#!/usr/bin/with-contenv bashio
# shellcheck shell=bash

bashio::log.info "######## Starting HAOSKiosk ########"
bashio::log.info "$(date) [Version: $ADDON_VERSION]"
bashio::log.info "$(uname -a)"
ha_info=$(bashio::info)
bashio::log.info "Core=$(echo "$ha_info" | jq -r '.homeassistant')  HAOS=$(echo "$ha_info" | jq -r '.hassos')  MACHINE=$(echo "$ha_info" | jq -r '.machine')  ARCH=$(echo "$ha_info" | jq -r '.arch')"

TTY0_DELETED=""
cleanup() {
    local exit_code=$?
    bashio::log.info "Cleaning up and exiting..."
    jobs -p | xargs -r kill
    [ -n "$TTY0_DELETED" ] && mknod -m 620 /dev/tty0 c 4 0
    rm -f /root/.local/share/luakit/cookies.db
    exit "$exit_code"
}
trap cleanup HUP INT QUIT ABRT TERM EXIT

BROWSER="luakit"
BROWSER_FLAGS=

load_config_var() {
    local VAR_NAME="$1"
    local DEFAULT="${2:-}"
    local MASK="${3:-}"
    local VALUE

    if declare -p "$VAR_NAME" >/dev/null 2>&1; then
        VALUE="${!VAR_NAME}"
    elif bashio::config.exists "${VAR_NAME,,}"; then
        VALUE="$(bashio::config "${VAR_NAME,,}")"
    else
        bashio::log.warning "Unknown config key: ${VAR_NAME,,}"
    fi

    if [ "$VALUE" = "null" ] || [ -z "$VALUE" ]; then
        bashio::log.warning "Config key '${VAR_NAME,,}' unset, setting to default: '$DEFAULT'"
        VALUE="$DEFAULT"
    fi

    printf -v "$VAR_NAME" '%s' "$VALUE"
    eval "export $VAR_NAME"

    if [ -z "$MASK" ]; then
        bashio::log.info "$VAR_NAME=$VALUE"
    else
        bashio::log.info "$VAR_NAME=XXXXXX"
    fi
}

load_config_var HA_USERNAME
load_config_var HA_PASSWORD "" 1
load_config_var HA_URL "http://localhost:8123"
load_config_var HA_DASHBOARD ""
load_config_var LOGIN_DELAY 1.0
load_config_var BROWSER_REFRESH 600
load_config_var SCREEN_TIMEOUT 600
load_config_var OUTPUT_NUMBER 1
load_config_var ROTATE_DISPLAY normal
load_config_var MAP_TOUCH_INPUTS true
load_config_var KEYBOARD_LAYOUT us
load_config_var DEBUG_MODE false
load_config_var SCREENSAVER_ENABLED false
load_config_var SCREENSAVER_TIMEOUT 300
load_config_var SCREENSAVER_INTERVAL 15
load_config_var SCREENSAVER_MEDIA_FOLDER screensaver

if [ -z "$HA_USERNAME" ] || [ -z "$HA_PASSWORD" ]; then
    bashio::log.error "Error: HA_USERNAME and HA_PASSWORD must be set"
    exit 1
fi

################################################################################
# GTK/DBUS-related environment variables to improve stability
export GTK_USE_PORTAL=0               # Disable portals
export GIO_USE_VFS=local              # Local-only GIO
export DBUS_SESSION_BUS_TIMEOUT=5000  # Shorten DBUS timeouts

################################################################################
# Start dbus so GTK/WebKit calls that expect a session bus don't hang/retry
DBUS_SESSION_BUS_ADDRESS=$(dbus-daemon --session --fork --print-address)
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    bashio::log.warning "WARNING: Failed to start dbus-daemon"
fi
export DBUS_SESSION_BUS_ADDRESS
bashio::log.info "DBus started with: DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"

################################################################################
# Hack to get writable /dev/tty0 for X
if [ -e "/dev/tty0" ]; then
    bashio::log.info "Remounting /dev as rw to delete /dev/tty0..."
    mount -o remount,rw /dev
    if ! mount -o remount,rw /dev ; then
        bashio::log.error "Failed to remount /dev as read-write..."
        exit 1
    fi
    if ! rm -f /dev/tty0 ; then
        bashio::log.error "Failed to delete /dev/tty0..."
        exit 1
    fi
    TTY0_DELETED=1
    bashio::log.info "Deleted /dev/tty0 successfully..."
fi

################################################################################
# Start udev
bashio::log.info "Starting udevd..."
if ! udevd --daemon || ! udevadm trigger; then
    bashio::log.warning "WARNING: Failed to start udevd, input devices may not work"
fi
udevadm settle --timeout=10

# Force tag event input devices for libinput
mapfile -t devices < <(find /dev/input/event* -type c 2>/dev/null | sort -V)
if [ ${#devices[@]} -eq 0 ]; then
    bashio::log.warning "WARNING: No input event devices found"
else
    for dev in "${devices[@]}"; do
        devpath=""
        for _ in {1..25}; do
            if devpath=$(udevadm info --query=path --name="$dev" 2>/dev/null); then
                break
            fi
            sleep 0.2
        done
        [ -z "$devpath" ] && continue
        udevadm test "$devpath" >/dev/null 2>&1 || true
    done
fi
udevadm settle --timeout=10

################################################################################
# Determine main display card
bashio::log.info "DRM video cards:"
find /dev/dri/ -maxdepth 1 -type c -name 'card[0-9]*' 2>/dev/null | sed 's/^/  /'
selected_card=""
for status_path in /sys/class/drm/card[0-9]*-*/status; do
    [ -e "$status_path" ] || continue
    status=$(cat "$status_path")
    card_port=$(basename "$(dirname "$status_path")")
    card=${card_port%%-*}
    driver=$(basename "$(readlink "/sys/class/drm/$card/device/driver")")
    if [ -z "$selected_card" ] && [ "$status" = "connected" ]; then
        selected_card="$card"
        printf "  *"
    else
        printf "   "
    fi
    printf "%-25s%-20s%s\n" "$card_port" "$driver" "$status"
done
if [ -z "$selected_card" ]; then
    bashio::log.error "ERROR: No connected video card detected. Exiting..."
    exit 1
fi

rm -rf /tmp/.X*-lock

# Build xorg.conf: copy default then add kmsdev for selected card
cp -a /etc/X11/xorg.conf{.default,}
sed -i "/Option[[:space:]]\+\"DRI\"[[:space:]]\+\"3\"/a\    Option     \t\t\"kmsdev\" \"/dev/dri/$selected_card\"" /etc/X11/xorg.conf

bashio::log.info "Starting X on DISPLAY=$DISPLAY..."
Xorg -nocursor </dev/null 2>&1 | grep -v "Could not resolve keysym XF86\|Errors from xkbcomp are not fatal\|XKEYBOARD keymap compiler (xkbcomp) reports" &

XSTARTUP=30
for ((i=0; i<=XSTARTUP; i++)); do
    if xset q >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Restore /dev/tty0
if [ -n "$TTY0_DELETED" ]; then
    mknod -m 620 /dev/tty0 c 4 0 2>/dev/null || true
fi

if ! xset q >/dev/null 2>&1; then
    bashio::log.error "Error: X server failed to start within $XSTARTUP seconds."
    exit 1
fi
bashio::log.info "X server started successfully after $i seconds..."

# Stop console blinking cursor
echo -e "\033[?25l" > /dev/console

################################################################################
# Start Openbox window manager (needed so luakit's fullscreen request is honored)
openbox &
O_PID=$!
sleep 0.5
if ! kill -0 "$O_PID" 2>/dev/null; then
    bashio::log.error "Failed to start Openbox window manager"
    exit 1
fi
bashio::log.info "Openbox window manager started successfully..."

################################################################################
# Configure screen timeout
xset +dpms
xset s "$SCREEN_TIMEOUT"
xset dpms "$SCREEN_TIMEOUT" "$SCREEN_TIMEOUT" "$SCREEN_TIMEOUT"
if [ "$SCREEN_TIMEOUT" -eq 0 ]; then
    bashio::log.info "Screen timeout disabled..."
else
    bashio::log.info "Screen timeout after $SCREEN_TIMEOUT seconds..."
fi

################################################################################
# Detect and configure outputs
readarray -t OUTPUTS < <(xrandr --query | awk '/ connected/ {print $1}')
if [ ${#OUTPUTS[@]} -eq 0 ]; then
    bashio::log.error "ERROR: No connected outputs detected. Exiting..."
    exit 1
fi

if [ "$OUTPUT_NUMBER" -gt "${#OUTPUTS[@]}" ]; then
    OUTPUT_NUMBER=${#OUTPUTS[@]}
fi

for i in "${!OUTPUTS[@]}"; do
    marker=" "
    [ "$i" -eq "$((OUTPUT_NUMBER - 1))" ] && marker="*"
    bashio::log.info "  ${marker}[$((i + 1))] ${OUTPUTS[$i]}"
done
OUTPUT_NAME="${OUTPUTS[$((OUTPUT_NUMBER - 1))]}"

for OUTPUT in "${OUTPUTS[@]}"; do
    if [ "$OUTPUT" = "$OUTPUT_NAME" ]; then
        if [ "$ROTATE_DISPLAY" = normal ]; then
            xrandr --output "$OUTPUT_NAME" --primary --auto
        else
            xrandr --output "$OUTPUT_NAME" --primary --rotate "${ROTATE_DISPLAY}"
            bashio::log.info "Rotating $OUTPUT_NAME: ${ROTATE_DISPLAY}"
        fi
    else
        xrandr --output "$OUTPUT" --off
    fi
done

# Map touch inputs
if [ "$MAP_TOUCH_INPUTS" = true ]; then
    while IFS= read -r id; do
        name=$(xinput list --name-only "$id" 2>/dev/null)
        [[ "${name,,}" =~ (^|[^[:alnum:]_])(touch|touchscreen|stylus)([^[:alnum:]_]|$) ]] || continue
        xinput_line=$(xinput list "$id" 2>/dev/null)
        [[ "$xinput_line" =~ \[(slave|master)[[:space:]]+keyboard[[:space:]]+\([0-9]+\)\] ]] && continue
        props="$(xinput list-props "$id" 2>/dev/null)"
        [[ "$props" = *"Coordinate Transformation Matrix"* ]] || continue
        xinput map-to-output "$id" "$OUTPUT_NAME" && RESULT="SUCCESS" || RESULT="FAILED"
        bashio::log.info "Mapping: input device [$id|$name] --> $OUTPUT_NAME [$RESULT]"
    done < <(xinput list --id-only | sort -n)
fi

# Set keyboard layout
setxkbmap "$KEYBOARD_LAYOUT"
export LANG=$KEYBOARD_LAYOUT
bashio::log.info "Setting keyboard layout to: $KEYBOARD_LAYOUT"

################################################################################
# Launch browser (or debug mode)
if [ "$DEBUG_MODE" != true ]; then
    $BROWSER ${BROWSER_FLAGS:+$BROWSER_FLAGS} "$HA_URL/$HA_DASHBOARD" &
    bashio::log.info "Launching $BROWSER (PID=$!): $HA_URL/$HA_DASHBOARD"

    count=0
    while true; do
        if pgrep -f -- "^$BROWSER " > /dev/null 2>&1; then
            count=0
        else
            count=$((count + 1))
        fi
        [ $count -ge 3 ] && break
        sleep 5
    done
    bashio::log.info "No $BROWSER instances remaining... exiting..."
else
    bashio::log.info "Debug mode: X running but no browser launched."
    exec sleep infinite
fi
