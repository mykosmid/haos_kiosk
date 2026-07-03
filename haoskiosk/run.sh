#!/usr/bin/with-contenv bashio
# shellcheck shell=bash

bashio::log.info "######## Starting HAOSKiosk ########"
bashio::log.info "$(date) [Version: $ADDON_VERSION]"
bashio::log.info "$(uname -a)"
ha_info=$(bashio::info)
bashio::log.info "Core=$(echo "$ha_info" | jq -r '.homeassistant')  HAOS=$(echo "$ha_info" | jq -r '.hassos')  MACHINE=$(echo "$ha_info" | jq -r '.machine')  ARCH=$(echo "$ha_info" | jq -r '.arch')"

SHUTDOWN=0
CHILD_PID=""
cleanup() {
    SHUTDOWN=1
    bashio::log.info "Shutting down..."
    [ -n "$CHILD_PID" ] && kill "$CHILD_PID" 2>/dev/null
}
trap cleanup HUP INT QUIT ABRT TERM

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

load_config_var HA_URL "http://localhost:8123"
load_config_var HA_TOKEN "" 1
load_config_var ENTITIES ""
load_config_var SHOPPING_LIST_ENTITY "todo.shopping_list"
load_config_var CHORES_ENTITIES ""
load_config_var DARK_MODE true
load_config_var DEBUG_MODE false
load_config_var MIN_FREE_MEMORY_MB 100

if [ -z "$HA_TOKEN" ] || [ -z "$ENTITIES" ]; then
    bashio::log.error "Error: HA_TOKEN and ENTITIES must be set"
    exit 1
fi

################################################################################
if [ "$DEBUG_MODE" = true ]; then
    bashio::log.info "Debug mode: sleeping without launching kiosk.py."
    bashio::log.info "Manually run: docker exec -it addon_haoskiosk python3 /kiosk.py"
    exec sleep infinity
fi

while [ "$SHUTDOWN" -eq 0 ]; do
    python3 /kiosk.py &
    CHILD_PID=$!
    bashio::log.info "Launched kiosk.py (PID=$CHILD_PID)"
    wait "$CHILD_PID"
    EXIT_CODE=$?
    CHILD_PID=""

    [ "$SHUTDOWN" -eq 1 ] && break

    bashio::log.warning "kiosk.py exited (code $EXIT_CODE) - restarting in 2s..."
    sleep 2
done
