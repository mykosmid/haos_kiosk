"""Native, browser-free Home Assistant kiosk display.

Renders a small grid of entity cards, plus two optional corner to-do
widgets - a read-only shopping list (bottom-right) and a tap-to-toggle
chores list (top-right, can combine multiple todo lists) - directly to the
Linux framebuffer (/dev/fb0) using Pillow, reads touch input directly from
/dev/input/eventN via evdev, and talks to Home Assistant over its WebSocket
API (ha_client.py) for state and tap-to-toggle control. No X11/GTK/WebKit
anywhere in this stack - the point is to use as little memory as possible on
a memory-constrained device (e.g. a 1GB Raspberry Pi).
"""

import collections
import fcntl
import logging
import math
import mmap
import os
import re
import select
import struct
import sys
import threading
import time

from evdev import InputDevice, ecodes, list_devices
from PIL import Image, ImageDraw, ImageFont

from ha_client import HAClient

log = logging.getLogger("kiosk")

EXIT_LOW_MEMORY = 42

# Domains that support a plain toggle service call on tap; anything else is
# rendered read-only.
TOGGLE_DOMAINS = {"light", "switch", "input_boolean", "fan"}

CARD_MARGIN = 12
TOGGLE_SIZE = 16
TAP_MOVE_THRESHOLD = 24  # px of finger movement before a touch is treated as a drag, not a tap

FONT_CANDIDATES = [
    os.environ.get("FONT_PATH", ""),
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

PALETTE_DARK = {
    "bg": (18, 18, 20),
    "card": (40, 40, 44),
    "card_on": (60, 110, 90),
    "text": (235, 235, 235),
    "accent": (140, 230, 190),
    "toggle_done": (90, 200, 130),
    "toggle_pending": (210, 80, 80),
}
PALETTE_LIGHT = {
    "bg": (240, 240, 240),
    "card": (255, 255, 255),
    "card_on": (200, 235, 220),
    "text": (30, 30, 30),
    "accent": (20, 130, 90),
    "toggle_done": (40, 160, 80),
    "toggle_pending": (195, 55, 55),
}

Card = collections.namedtuple("Card", ["rect", "entity_id", "domain"])


# ---------------------------------------------------------------------------
# Framebuffer output
# ---------------------------------------------------------------------------

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602

# struct fb_var_screeninfo is entirely __u32 fields, so this layout is stable
# across 32-bit and 64-bit architectures.
_VSCREENINFO_FMT = "40I"

# struct fb_fix_screeninfo contains 'unsigned long' fields whose size depends
# on the architecture's word size. No byte-order prefix is used here on
# purpose: that puts struct in "native size, native alignment" mode, which is
# what's needed to match the kernel's own in-memory layout for this arch.
_FSCREENINFO_FMT = "16sLIIIHHHILIIH2H"

# Buffer passed to ioctl() is intentionally larger than either struct's real
# size. The kernel only ever writes the size baked into the ioctl request
# code, but sizing the format strings above by hand for every architecture
# carries real risk of being subtly wrong - padding the buffer means a
# too-small guess just leaves the tail unused instead of corrupting memory.
_IOCTL_BUF_SIZE = 256


class Framebuffer:
    def __init__(self, device="/dev/fb0"):
        self.fd = os.open(device, os.O_RDWR)

        vinfo = self._ioctl_struct(FBIOGET_VSCREENINFO, _VSCREENINFO_FMT)
        (
            self.xres, self.yres, self.xres_virtual, self.yres_virtual,
            _xoffset, _yoffset, self.bpp, _grayscale,
            red_offset, red_length, _red_msb,
            green_offset, green_length, _green_msb,
            blue_offset, blue_length, _blue_msb,
            *_rest,
        ) = vinfo

        finfo = self._ioctl_struct(FBIOGET_FSCREENINFO, _FSCREENINFO_FMT)
        smem_len = finfo[2]
        self.line_length = finfo[9]

        bytes_per_pixel = self.bpp // 8
        expected_line_length = self.xres_virtual * bytes_per_pixel
        if self.line_length == 0 or not (expected_line_length <= self.line_length <= expected_line_length * 2):
            log.warning(
                "Framebuffer line_length (%d) looks implausible for %dx%d@%dbpp; "
                "using computed stride %d instead",
                self.line_length, self.xres_virtual, self.yres, self.bpp, expected_line_length,
            )
            self.line_length = expected_line_length

        self.map_size = self.line_length * self.yres_virtual
        if smem_len and smem_len < self.map_size:
            self.map_size = smem_len

        self.rawmode = _detect_rawmode(
            self.bpp, (red_offset, red_length), (green_offset, green_length), (blue_offset, blue_length)
        )

        self.mm = mmap.mmap(self.fd, self.map_size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        log.info(
            "Framebuffer %s: %dx%d @ %dbpp, stride=%d, rawmode=%s",
            device, self.xres, self.yres, self.bpp, self.line_length, self.rawmode,
        )

    def _ioctl_struct(self, request, fmt):
        buf = bytearray(_IOCTL_BUF_SIZE)
        fcntl.ioctl(self.fd, request, buf)
        return struct.unpack_from(fmt, buf)

    def blit(self, image):
        """Write a PIL Image sized exactly (self.xres, self.yres) to the screen."""
        if self.rawmode == "RGB565":
            # Pillow can decode RGB565 ("BGR;16") but has no encoder for it -
            # tobytes("raw", "BGR;16") raises "No packer found" - so this one
            # format has to be packed by hand instead of via Pillow's raw codec.
            raw = _pack_rgb565(image.tobytes("raw", "RGB"))
        else:
            raw = image.tobytes("raw", self.rawmode)
        bytes_per_pixel = self.bpp // 8
        row_bytes = self.xres * bytes_per_pixel
        if self.line_length == row_bytes:
            self.mm.seek(0)
            self.mm.write(raw)
        else:
            for y in range(self.yres):
                self.mm.seek(y * self.line_length)
                self.mm.write(raw[y * row_bytes:(y + 1) * row_bytes])

    def close(self):
        try:
            self.mm.close()
        finally:
            os.close(self.fd)


def _detect_rawmode(bpp, red, green, blue):
    """Map the framebuffer's reported channel layout to a pixel-packing mode.

    Returns either a Pillow 'raw' rawmode string (for formats Pillow's raw
    codec can produce directly, i.e. any byte ordering of 32bpp RGB/BGR) or
    the sentinel "RGB565", which Framebuffer.blit() packs by hand since
    Pillow only supports decoding that format, not encoding it.
    """
    if bpp == 16 and red == (11, 5) and green == (5, 6) and blue == (0, 5):
        return "RGB565"

    if bpp == 32 and all(offset % 8 == 0 for offset, _length in (red, green, blue)):
        byte_for_pos = {red[0] // 8: "R", green[0] // 8: "G", blue[0] // 8: "B"}
        if len(byte_for_pos) == 3:
            return "".join(byte_for_pos.get(pos, "X") for pos in range(4))

    raise RuntimeError(
        f"Unsupported framebuffer pixel format: bpp={bpp} red={red} green={green} blue={blue}. "
        "Only 32bpp RGB/BGR (any byte order) and standard 16bpp RGB565 are supported."
    )


def _pack_rgb565(rgb_bytes):
    """Pack raw 8-bit-per-channel RGB bytes into little-endian RGB565.

    Done a pixel at a time in pure Python since Pillow has no RGB565 encoder
    (see the comment in Framebuffer.blit()) and this project intentionally
    avoids adding numpy as a dependency just for this one uncommon format.
    Only runs on redraw (state change or tap), not continuously, so the cost
    of this loop is a one-off few hundred ms on affected hardware rather than
    a per-frame cost.
    """
    out = bytearray(len(rgb_bytes) // 3 * 2)
    for i in range(0, len(rgb_bytes) - 2, 3):
        r, g, b = rgb_bytes[i], rgb_bytes[i + 1], rgb_bytes[i + 2]
        value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        j = i // 3 * 2
        out[j] = value & 0xFF
        out[j + 1] = value >> 8
    return bytes(out)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class Renderer:
    def __init__(self, width, height, entity_ids, dark_mode, shopping_list_entity=None, chores_entities=None):
        self.width = width
        self.height = height
        self.entity_ids = entity_ids
        self.palette = PALETTE_DARK if dark_mode else PALETTE_LIGHT
        self.font_name = _load_font(FONT_CANDIDATES, 22)
        self.font_state = _load_font(FONT_BOLD_CANDIDATES + FONT_CANDIDATES, 30)
        self.font_small = _load_font(FONT_CANDIDATES, 18)

        self.widgets = []
        if shopping_list_entity:
            self.widgets.append({
                "entity_ids": [shopping_list_entity], "title": "Shopping List",
                "corner": "bottom-right", "interactive": False,
            })
        if chores_entities:
            self.widgets.append({
                "entity_ids": chores_entities, "title": "Chores",
                "corner": "top-right", "interactive": True,
            })

        total_slots = len(entity_ids) + len(self.widgets)
        self.cols = 3 if total_slots > 6 else 2
        self.rows = math.ceil(total_slots / self.cols) if total_slots else 1
        # Two widgets in different corners need at least two rows to actually
        # land in different cells, even if the entity count alone wouldn't
        # otherwise require it.
        if any(w["corner"] == "top-right" for w in self.widgets) and any(
            w["corner"] == "bottom-right" for w in self.widgets
        ):
            self.rows = max(self.rows, 2)

        # Each widget always occupies its designated corner cell of the grid,
        # regardless of how many entities there are - entities fill the
        # remaining cells in order, skipping over any reserved ones.
        for widget in self.widgets:
            widget["reserved_index"] = (
                self.cols - 1 if widget["corner"] == "top-right" else self.rows * self.cols - 1
            )

    def render(self, client):
        img = Image.new("RGB", (self.width, self.height), self.palette["bg"])
        draw = ImageDraw.Draw(img)

        cards = []
        cell_w = self.width // self.cols
        cell_h = self.height // self.rows
        reserved_indices = {w["reserved_index"] for w in self.widgets}

        cell_index = 0
        for entity_id in self.entity_ids:
            while cell_index in reserved_indices:
                cell_index += 1
            rect = self._cell_rect(cell_index, cell_w, cell_h)
            x0, y0, x1, y1 = rect

            state = client.get_state(entity_id)
            domain = entity_id.split(".", 1)[0]
            is_on = bool(state) and state.get("state") == "on"

            draw.rounded_rectangle(
                [x0, y0, x1, y1], radius=14,
                fill=self.palette["card_on"] if is_on else self.palette["card"],
            )

            name = _friendly_name(state, entity_id)
            state_text = _format_state(state)

            draw.text((x0 + 16, y0 + 14), name, font=self.font_name, fill=self.palette["text"])
            draw.text(
                (x0 + 16, y1 - 46), state_text, font=self.font_state,
                fill=self.palette["accent"] if is_on else self.palette["text"],
            )

            cards.append(Card(rect=rect, entity_id=entity_id, domain=domain))
            cell_index += 1

        widget_hits = []
        for config in self.widgets:
            rect = self._cell_rect(config["reserved_index"], cell_w, cell_h)
            item_hits = self._draw_todo_widget(draw, rect, client, config)
            widget_hits.append({
                "rect": rect, "interactive": config["interactive"], "item_hits": item_hits,
            })

        return img, cards, widget_hits

    def _cell_rect(self, cell_index, cell_w, cell_h):
        row, col = divmod(cell_index, self.cols)
        x0, y0 = col * cell_w + CARD_MARGIN, row * cell_h + CARD_MARGIN
        x1, y1 = (col + 1) * cell_w - CARD_MARGIN, (row + 1) * cell_h - CARD_MARGIN
        return x0, y0, x1, y1

    def _draw_todo_widget(self, draw, rect, client, config):
        """Draws one corner to-do widget and returns a list of item-hit dicts
        (rect/entity_id/uid/status) for tap-to-toggle - empty unless
        config["interactive"]. Non-interactive widgets only show items that
        aren't complete yet; interactive ones show everything so a completed
        item's toggle square stays visible (green) and can be tapped again to
        reopen it."""
        x0, y0, x1, y1 = rect
        draw.rounded_rectangle([x0, y0, x1, y1], radius=14, fill=self.palette["card"])
        draw.text((x0 + 16, y0 + 14), config["title"], font=self.font_name, fill=self.palette["text"])

        interactive = config["interactive"]
        items = []
        for entity_id in config["entity_ids"]:
            for item in client.get_todo_items(entity_id):
                if interactive or item.get("status") != "completed":
                    items.append({**item, "_entity_id": entity_id})

        top_y = y0 + 50
        bottom_y = y1 - 12
        return self._draw_items(draw, items, x0, x1, top_y, bottom_y, self.font_small, 26, interactive)

    def _draw_items(self, draw, items, x0, x1, top_y, bottom_y, item_font, line_height, interactive):
        item_hits = []
        prefix_width = TOGGLE_SIZE + 8 if interactive else 0
        text_x = x0 + 16 + prefix_width

        y = top_y
        max_lines = max(0, (bottom_y - y) // line_height)
        visible, overflow = items[:max_lines], max(0, len(items) - max_lines)
        if overflow and visible:
            # Reserve the last visible line for the "+N more" count instead of
            # a full item, so it's never drawn past the widget's bottom edge.
            visible, overflow = items[:max_lines - 1], len(items) - (max_lines - 1)

        for item in visible:
            status = item.get("status")
            if interactive:
                self._draw_toggle_square(draw, x0 + 16, y, status == "completed")
            text = _truncate_text(draw, item.get("summary", ""), item_font, (x1 - 16) - text_x)
            prefix = "" if interactive else "• "
            draw.text((text_x, y), f"{prefix}{text}", font=item_font, fill=self.palette["text"])
            if interactive:
                item_hits.append({
                    "rect": (x0, y - 4, x1, y + line_height - 4),
                    "entity_id": item.get("_entity_id"),
                    "uid": item.get("uid"),
                    "status": status,
                })
            y += line_height

        if not items:
            draw.text((x0 + 16, y), "(empty)", font=item_font, fill=self.palette["text"])
        elif overflow:
            draw.text((x0 + 16, y), f"+{overflow} more", font=item_font, fill=self.palette["accent"])

        return item_hits

    def _draw_toggle_square(self, draw, x, y, completed):
        color = self.palette["toggle_done"] if completed else self.palette["toggle_pending"]
        draw.rounded_rectangle([x, y + 2, x + TOGGLE_SIZE, y + 2 + TOGGLE_SIZE], radius=3, fill=color)


def _friendly_name(state, entity_id):
    if state:
        name = state.get("attributes", {}).get("friendly_name")
        if name:
            return name
    return entity_id


def _format_state(state):
    if not state:
        return "unavailable"
    value = state.get("state", "unknown")
    unit = state.get("attributes", {}).get("unit_of_measurement")
    return f"{value} {unit}" if unit else value


def _truncate_text(draw, text, font, max_width):
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…" if text else "…"


def _load_font(candidates, size):
    for path in candidates:
        if not path:
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    log.warning("No TrueType font found (tried: %s); falling back to a tiny built-in bitmap font", candidates)
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Touch input
# ---------------------------------------------------------------------------

class TouchInput:
    """Reads raw touch events from evdev devices and reports completed taps.

    Scales each device's reported coordinate range to the framebuffer's pixel
    space using the device's own advertised ABS min/max, since a touch
    controller's raw range rarely matches the panel's pixel resolution.
    """

    def __init__(self, screen_w, screen_h, device_paths=None):
        self.screen_w = screen_w
        self.screen_h = screen_h
        self._devices = {}  # fd -> _DeviceState

        paths = device_paths or list_devices()
        for path in paths:
            try:
                dev = InputDevice(path)
            except OSError as exc:
                log.warning("Could not open input device %s: %s", path, exc)
                continue

            caps = dev.capabilities().get(ecodes.EV_ABS, [])
            abs_codes = {code for code, _info in caps}
            if ecodes.ABS_MT_POSITION_X in abs_codes:
                x_code, y_code = ecodes.ABS_MT_POSITION_X, ecodes.ABS_MT_POSITION_Y
            elif ecodes.ABS_X in abs_codes:
                x_code, y_code = ecodes.ABS_X, ecodes.ABS_Y
            else:
                continue  # not a touch/pointer device

            x_info = dev.absinfo(x_code)
            y_info = dev.absinfo(y_code)
            log.info(
                "Using touch input device: %s (%s) range x=[%d,%d] y=[%d,%d]",
                dev.path, dev.name, x_info.min, x_info.max, y_info.min, y_info.max,
            )
            self._devices[dev.fd] = _DeviceState(dev, x_code, y_code, x_info, y_info)

        if not self._devices:
            log.warning("No touch input devices found - taps will not work")

    def fds(self):
        return list(self._devices.keys())

    def process(self, fd):
        """Consume pending events for one ready fd; returns a (x, y) screen
        coordinate tap on touch-release, or None."""
        state = self._devices[fd]
        tap = None
        for event in state.dev.read():
            if event.type == ecodes.EV_ABS:
                if event.code == state.x_code:
                    state.x = event.value
                elif event.code == state.y_code:
                    state.y = event.value
            elif event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH:
                if event.value == 1:
                    state.down = True
                    state.start_x, state.start_y = state.x, state.y
                elif event.value == 0 and state.down:
                    state.down = False
                    if state.start_x is not None and state.x is not None:
                        moved = abs(state.x - state.start_x) + abs(state.y - state.start_y)
                        if moved < TAP_MOVE_THRESHOLD:
                            tap = self._scale(state)
        return tap

    def _scale(self, state):
        x = _scale_axis(state.x, state.x_info, self.screen_w)
        y = _scale_axis(state.y, state.y_info, self.screen_h)
        return x, y


class _DeviceState:
    def __init__(self, dev, x_code, y_code, x_info, y_info):
        self.dev = dev
        self.x_code = x_code
        self.y_code = y_code
        self.x_info = x_info
        self.y_info = y_info
        self.x = self.y = None
        self.start_x = self.start_y = None
        self.down = False


def _scale_axis(value, absinfo, screen_size):
    span = absinfo.max - absinfo.min
    if span <= 0 or value is None:
        return 0
    return max(0, min(screen_size - 1, round((value - absinfo.min) / span * screen_size)))


def _point_in_rect(pos, rect):
    x, y = pos
    x0, y0, x1, y1 = rect
    return x0 <= x <= x1 and y0 <= y <= y1


def handle_tap(pos, cards, client):
    for card in cards:
        if _point_in_rect(pos, card.rect):
            if card.domain in TOGGLE_DOMAINS:
                log.info("Tap at %s: toggling %s", pos, card.entity_id)
                client.call_service(card.domain, "toggle", card.entity_id)
            else:
                log.debug("Tap at %s on read-only card: %s", pos, card.entity_id)
            return


def handle_widget_tap(pos, widgets, client):
    """Returns True if a corner to-do widget consumed the tap - i.e. the tap
    landed inside a widget's rect at all - False if it should fall through to
    the entity grid instead. Tapping an item's toggle square on an
    interactive widget flips it between "needs_action" and "completed" in
    Home Assistant; tapping elsewhere in a widget does nothing."""
    for widget in widgets:
        if not _point_in_rect(pos, widget["rect"]):
            continue
        if widget["interactive"]:
            hit = next((h for h in widget["item_hits"] if _point_in_rect(pos, h["rect"])), None)
            if hit:
                new_status = "needs_action" if hit["status"] == "completed" else "completed"
                log.info("Tap at %s: setting %s on %s to %s", pos, hit["uid"], hit["entity_id"], new_status)
                client.set_todo_item_status(hit["entity_id"], hit["uid"], new_status)
        return True
    return False


# ---------------------------------------------------------------------------
# Low-memory watchdog
# ---------------------------------------------------------------------------

_MEMINFO_RE = re.compile(rb"MemAvailable:\s+(\d+)")


def memory_watchdog(threshold_mb, stop_event, check_interval=20):
    if threshold_mb <= 0:
        log.info("Low-memory watchdog disabled (min_free_memory_mb=0)")
        return
    log.info("Low-memory watchdog started: restarting if available memory drops below %d MB", threshold_mb)
    while not stop_event.wait(check_interval):
        try:
            with open("/proc/meminfo", "rb") as f:
                match = _MEMINFO_RE.search(f.read())
            if not match:
                continue
            available_mb = int(match.group(1)) // 1024
            if available_mb < threshold_mb:
                log.error(
                    "Low memory: %d MB available < %d MB threshold; exiting for a clean restart",
                    available_mb, threshold_mb,
                )
                os._exit(EXIT_LOW_MEMORY)
        except Exception:
            log.exception("Memory watchdog check failed")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    ha_url = os.environ.get("HA_URL", "http://localhost:8123")
    ha_token = os.environ.get("HA_TOKEN", "")
    entities = [e.strip() for e in os.environ.get("ENTITIES", "").split(",") if e.strip()]
    dark_mode = os.environ.get("DARK_MODE", "true").strip().lower() == "true"
    min_free_memory_mb = int(os.environ.get("MIN_FREE_MEMORY_MB", "100") or 0)
    shopping_list_entity = os.environ.get("SHOPPING_LIST_ENTITY", "todo.shopping_list").strip()
    if shopping_list_entity.lower() in ("", "none"):
        shopping_list_entity = None
    chores_entities = [
        e.strip() for e in os.environ.get("CHORES_ENTITIES", "").split(",")
        if e.strip() and e.strip().lower() != "none"
    ]

    if not ha_token:
        log.error("HA_TOKEN is not set - cannot authenticate to Home Assistant")
        sys.exit(1)
    if not entities:
        log.error("ENTITIES is empty - nothing configured to display")
        sys.exit(1)

    log.info("Entities: %s", ", ".join(entities))
    if shopping_list_entity:
        log.info("Shopping list widget: %s", shopping_list_entity)
    if chores_entities:
        log.info("Chores widget: %s", ", ".join(chores_entities))

    stop_event = threading.Event()
    threading.Thread(
        target=memory_watchdog, args=(min_free_memory_mb, stop_event), name="mem-watchdog", daemon=True
    ).start()

    fb = Framebuffer(os.environ.get("FB_DEVICE", "/dev/fb0"))
    renderer = Renderer(fb.xres, fb.yres, entities, dark_mode, shopping_list_entity, chores_entities)
    touch = TouchInput(fb.xres, fb.yres)

    dirty = threading.Event()
    dirty.set()  # draw once immediately

    todo_entity_ids = ([shopping_list_entity] if shopping_list_entity else []) + chores_entities
    client = HAClient(
        ha_url, ha_token, entities, todo_entity_ids=todo_entity_ids,
        on_update=lambda _entity_id: dirty.set(),
    )
    client.start()

    last_cards = []
    last_widgets = []
    try:
        while True:
            if dirty.is_set():
                dirty.clear()
                image, last_cards, last_widgets = renderer.render(client)
                fb.blit(image)

            fds = touch.fds()
            readable, _, _ = select.select(fds, [], [], 0.5) if fds else ([], [], [])
            if not fds:
                time.sleep(0.5)

            for fd in readable:
                tap = touch.process(fd)
                if tap:
                    if handle_widget_tap(tap, last_widgets, client):
                        dirty.set()
                    else:
                        handle_tap(tap, last_cards, client)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        fb.close()


if __name__ == "__main__":
    main()
