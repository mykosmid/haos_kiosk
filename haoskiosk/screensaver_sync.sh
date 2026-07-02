#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
#
# Watches $SCREENSAVER_SOURCE_FOLDER (a folder under HA's local Media root,
# "/media") for image files and mirrors resized copies into
# $SCREENSAVER_MEDIA_FOLDER, so images uploaded via the HA Media page don't
# need to be resized by hand before showing up in the kiosk screensaver.

SOURCE_DIR="/media${SCREENSAVER_SOURCE_FOLDER:+/$SCREENSAVER_SOURCE_FOLDER}"
DEST_DIR="/media${SCREENSAVER_MEDIA_FOLDER:+/$SCREENSAVER_MEDIA_FOLDER}"
RESIZE_GEOMETRY="${SCREENSAVER_RESIZE_WIDTH}x${SCREENSAVER_RESIZE_HEIGHT}>"

if [ "$SOURCE_DIR" = "$DEST_DIR" ]; then
    bashio::log.error "Screensaver sync: source and destination folders must differ (both resolve to '$SOURCE_DIR')"
    exit 1
fi

mkdir -p "$SOURCE_DIR" "$DEST_DIR"

convert_image() {
    local src="$1" base out
    [ -f "$src" ] || return
    base="$(basename "$src")"
    case "${base,,}" in
        *.jpg | *.jpeg | *.png | *.gif | *.bmp | *.webp) ;;
        *) return ;; # Skip non-image files
    esac

    out="$DEST_DIR/${base%.*}.jpg"
    if [ -e "$out" ] && [ "$out" -nt "$src" ]; then
        return # Already converted and up to date
    fi

    if convert "$src" -auto-orient -resize "$RESIZE_GEOMETRY" -strip -quality 85 "$out" 2>/tmp/screensaver_sync.err; then
        bashio::log.info "Screensaver sync: converted '$base' -> '$out'"
    else
        bashio::log.warning "Screensaver sync: failed to convert '$src': $(cat /tmp/screensaver_sync.err)"
    fi
}

bashio::log.info "Screensaver sync: watching '$SOURCE_DIR' -> '$DEST_DIR' (resize: $RESIZE_GEOMETRY)"

# Initial pass over any images already present
find "$SOURCE_DIR" -maxdepth 1 -type f | while IFS= read -r f; do
    convert_image "$f"
done

# Watch for new/replaced files (non-recursive: only direct children of SOURCE_DIR,
# so writes into a DEST_DIR nested under SOURCE_DIR are never picked back up)
while true; do
    inotifywait -q -m -e close_write -e moved_to --format '%f' "$SOURCE_DIR" 2>/dev/null |
        while IFS= read -r filename; do
            convert_image "$SOURCE_DIR/$filename"
        done
    bashio::log.warning "Screensaver sync: inotifywait exited unexpectedly, restarting in 5s..."
    sleep 5
done
