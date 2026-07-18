"""Native, browser-free Home Assistant kiosk display.

Renders a small grid of entity cards, plus two optional to-do widgets, each
a fixed quarter (or half, if the other is absent) of the screen - a
read-only shopping list (bottom-right, tap to expand full-screen) and a
tap-to-toggle chores list (top-right, can combine multiple todo lists) -
directly to the Linux framebuffer (/dev/fb0) using Pillow, reads touch input
directly from /dev/input/eventN via evdev, and talks to Home Assistant over
its WebSocket API (ha_client.py) for state and tap-to-toggle control. No
X11/GTK/WebKit anywhere in this stack - the point is to use as little memory
as possible on a memory-constrained device (e.g. a 1GB Raspberry Pi).
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

# Light cards get "-"/"+" buttons that nudge brightness via light.turn_on's
# brightness_step_pct, rather than setting an absolute level - that way
# repeated taps behave the same regardless of the light's current brightness.
BRIGHTNESS_BUTTON_SIZE = 44
BRIGHTNESS_STEP_PCT = 10

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

Card = collections.namedtuple("Card", ["rect", "entity_id", "domain", "buttons"], defaults=[{}])


# ---------------------------------------------------------------------------
# Framebuffer output
# ---------------------------------------------------------------------------

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602

# FBIOBLANK (see <linux/fb.h>) powers the panel down/up through the fb device
# itself. Unlike /sys/class/backlight, this works over the already-open
# /dev/fb0 fd, so it doesn't need a writable /sys (which the add-on container
# doesn't have - those nodes are on a read-only mount).
FBIOBLANK = 0x4611
FB_BLANK_UNBLANK = 0
FB_BLANK_POWERDOWN = 4

# Linux VT ioctl (see <linux/kd.h>) used to hide the console cursor.
KDSETMODE = 0x4B3A
KD_TEXT = 0x00
KD_GRAPHICS = 0x01

_CONSOLE_CANDIDATES = ["/dev/tty0", "/dev/console", "/dev/tty1"]


def hide_console_cursor():
    """Switches the active Linux virtual console into graphics mode, so the
    kernel's own fbcon driver stops blinking its text cursor on top of
    whatever this app draws to /dev/fb0 by hand. Returns the console fd (to
    later pass to restore_console_cursor()), or None if no console device
    was accessible - not fatal, just means the cursor may stay visible."""
    for path in _CONSOLE_CANDIDATES:
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            continue
        try:
            fcntl.ioctl(fd, KDSETMODE, KD_GRAPHICS)
            log.info("Console %s switched to graphics mode (hides the blinking cursor)", path)
            return fd
        except OSError as exc:
            log.warning("Could not switch console %s to graphics mode: %s", path, exc)
            os.close(fd)
    log.warning(
        "No accessible console device (tried: %s) - the console cursor may remain visible over the display",
        ", ".join(_CONSOLE_CANDIDATES),
    )
    return None


def restore_console_cursor(fd):
    if fd is None:
        return
    try:
        fcntl.ioctl(fd, KDSETMODE, KD_TEXT)
    except OSError:
        log.exception("Failed to restore console text mode")
    finally:
        os.close(fd)

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

    def blank(self, blank):
        """Power the panel down (blank=True) or back up via FBIOBLANK on the
        framebuffer fd. Whether this cuts the backlight depends on the panel
        driver, but on the Pi's KMS fbdev emulation FB_BLANK_POWERDOWN drives
        the display's power state off. Best-effort: warns once if the driver
        rejects the ioctl, then stays quiet so it can't spam the log on every
        night-mode transition."""
        try:
            fcntl.ioctl(self.fd, FBIOBLANK, FB_BLANK_POWERDOWN if blank else FB_BLANK_UNBLANK)
        except OSError as exc:
            if not getattr(self, "_blank_unsupported", False):
                self._blank_unsupported = True
                log.warning("Framebuffer FBIOBLANK not supported by this driver (%s); "
                            "night mode will show a black screen without powering the panel off", exc)

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
        self.expanded = False  # True while the shopping list is shown full-screen

        self.widgets = []
        if shopping_list_entity:
            self.widgets.append({
                "entity_ids": [shopping_list_entity], "title": "Shopping List",
                "corner": "bottom-right", "interactive": False, "expandable": True,
            })
        if chores_entities:
            self.widgets.append({
                "entity_ids": chores_entities, "title": "Chores",
                "corner": "top-right", "interactive": True, "expandable": False,
            })

        # Each configured widget is a fixed quarter of the screen (half width,
        # half height) in its designated corner - not sized off however many
        # entities are configured - so there's always enough room to show a
        # full list without truncating. If only one widget is configured it
        # gets the whole right half instead of just its quarter, since
        # there's no other widget to share that space with.
        both_present = any(w["corner"] == "top-right" for w in self.widgets) and any(
            w["corner"] == "bottom-right" for w in self.widgets
        )
        for widget in self.widgets:
            if widget["corner"] == "top-right":
                y1 = height // 2 if both_present else height
                widget["rect"] = (width // 2 + CARD_MARGIN, CARD_MARGIN, width - CARD_MARGIN, y1 - CARD_MARGIN)
            else:
                y0 = height // 2 if both_present else 0
                widget["rect"] = (width // 2 + CARD_MARGIN, y0 + CARD_MARGIN, width - CARD_MARGIN, height - CARD_MARGIN)

        # The entity grid is confined to the left half of the screen whenever
        # any widget is shown (freeing the whole right half for widgets), and
        # spans the full screen when neither is configured.
        self.entity_area = (0, 0, width // 2 if self.widgets else width, height)
        # 2 or fewer entities stack into a single column (one card per row)
        # instead of sitting side by side, since a lone pair of lights reads
        # better as two stacked horizontal bars than two tall vertical ones.
        self.cols = 1 if len(entity_ids) <= 2 else (3 if len(entity_ids) > 6 else 2)
        self.rows = math.ceil(len(entity_ids) / self.cols) if entity_ids else 1

    def render(self, client):
        img = Image.new("RGB", (self.width, self.height), self.palette["bg"])
        draw = ImageDraw.Draw(img)

        if self.expanded:
            config = next(w for w in self.widgets if w["expandable"])
            rect = (CARD_MARGIN, CARD_MARGIN, self.width - CARD_MARGIN, self.height - CARD_MARGIN)
            item_hits = self._draw_todo_widget(draw, rect, client, config, expanded=True)
            widget_hit = {
                "rect": rect, "interactive": config["interactive"],
                "expandable": True, "item_hits": item_hits,
            }
            return img, [], [widget_hit]

        cards = []
        ex0, ey0, ex1, ey1 = self.entity_area
        cell_w = (ex1 - ex0) // self.cols
        cell_h = (ey1 - ey0) // self.rows

        for index, entity_id in enumerate(self.entity_ids):
            rect = self._cell_rect(index, cell_w, cell_h)
            x0, y0, x1, y1 = rect

            state = client.get_state(entity_id)
            domain = entity_id.split(".", 1)[0]
            is_on = bool(state) and state.get("state") == "on"

            draw.rounded_rectangle(
                [x0, y0, x1, y1], radius=14,
                fill=self.palette["card_on"] if is_on else self.palette["card"],
            )

            name = _friendly_name(state, entity_id)
            state_text = _format_state(state, domain)

            draw.text((x0 + 16, y0 + 14), name, font=self.font_name, fill=self.palette["text"])
            draw.text(
                (x0 + 16, y1 - 46), state_text, font=self.font_state,
                fill=self.palette["accent"] if is_on else self.palette["text"],
            )

            buttons = self._draw_brightness_buttons(draw, rect, is_on) if domain == "light" else {}
            cards.append(Card(rect=rect, entity_id=entity_id, domain=domain, buttons=buttons))

        widget_hits = []
        for config in self.widgets:
            item_hits = self._draw_todo_widget(draw, config["rect"], client, config)
            widget_hits.append({
                "rect": config["rect"], "interactive": config["interactive"],
                "expandable": config["expandable"], "item_hits": item_hits,
            })

        return img, cards, widget_hits

    def _cell_rect(self, cell_index, cell_w, cell_h):
        row, col = divmod(cell_index, self.cols)
        x0, y0 = col * cell_w + CARD_MARGIN, row * cell_h + CARD_MARGIN
        x1, y1 = (col + 1) * cell_w - CARD_MARGIN, (row + 1) * cell_h - CARD_MARGIN
        return x0, y0, x1, y1

    def _draw_todo_widget(self, draw, rect, client, config, expanded=False):
        """Draws one to-do widget (its normal quarter/half-screen box, or a
        full-screen expanded view for config["expandable"] widgets) and
        returns a list of item-hit dicts (rect/entity_id/uid/status) for
        tap-to-toggle - empty unless config["interactive"]. Non-interactive
        widgets only show items that aren't complete yet; interactive ones
        show everything so a completed item's toggle square stays visible
        (green) and can be tapped again to reopen it."""
        x0, y0, x1, y1 = rect
        draw.rounded_rectangle([x0, y0, x1, y1], radius=14, fill=self.palette["card"])

        item_font = self.font_state if expanded else self.font_name
        line_height = 36 if expanded else 30
        title_gap = 60 if expanded else 54

        draw.text((x0 + 16, y0 + 14), config["title"], font=self.font_state, fill=self.palette["text"])

        interactive = config["interactive"]
        items = []
        for entity_id in config["entity_ids"]:
            for item in client.get_todo_items(entity_id):
                if interactive or item.get("status") != "completed":
                    items.append({**item, "_entity_id": entity_id})

        top_y = y0 + title_gap
        bottom_y = y1 - 12
        return self._draw_items(draw, items, x0, x1, top_y, bottom_y, item_font, line_height, interactive)

    def _draw_items(self, draw, items, x0, x1, top_y, bottom_y, item_font, line_height, interactive):
        """Lay out items in a single column, wrapping into a second column if
        they don't all fit, before ever falling back to a "+N more" count."""
        item_hits = []
        if not items:
            draw.text((x0 + 16, top_y), "(empty)", font=item_font, fill=self.palette["text"])
            return item_hits

        prefix_width = TOGGLE_SIZE + 8 if interactive else 0
        max_lines_per_col = max(1, (bottom_y - top_y) // line_height)

        def emit_hit(item, rect):
            if interactive:
                item_hits.append({
                    "rect": rect, "entity_id": item.get("_entity_id"),
                    "uid": item.get("uid"), "status": item.get("status"),
                })

        if len(items) <= max_lines_per_col:
            y = top_y
            for item in items:
                if interactive:
                    self._draw_toggle_square(draw, x0 + 16, y, item.get("status") == "completed")
                text = _truncate_text(draw, item.get("summary", ""), item_font, (x1 - 16) - (x0 + 16 + prefix_width))
                prefix = "" if interactive else "• "
                draw.text((x0 + 16 + prefix_width, y), f"{prefix}{text}", font=item_font, fill=self.palette["text"])
                emit_hit(item, (x0, y - 4, x1, y + line_height - 4))
                y += line_height
            return item_hits

        col_gap = 24
        col_width = (x1 - x0 - 32 - col_gap) // 2
        col_x = [x0 + 16, x0 + 16 + col_width + col_gap]

        capacity = max_lines_per_col * 2
        visible, overflow = items[:capacity], max(0, len(items) - capacity)
        if overflow:
            # Reserve the last slot (bottom of the second column) for the
            # "+N more" count instead of a full item.
            visible, overflow = items[:capacity - 1], len(items) - (capacity - 1)

        for index, item in enumerate(visible):
            col, row = divmod(index, max_lines_per_col)
            base_x = col_x[col]
            y = top_y + row * line_height
            if interactive:
                self._draw_toggle_square(draw, base_x, y, item.get("status") == "completed")
            text = _truncate_text(draw, item.get("summary", ""), item_font, col_width - prefix_width - 8)
            prefix = "" if interactive else "• "
            draw.text((base_x + prefix_width, y), f"{prefix}{text}", font=item_font, fill=self.palette["text"])
            col_right = col_x[col] + col_width if col == 0 else x1
            emit_hit(item, (base_x, y - 4, col_right, y + line_height - 4))

        if overflow:
            col, row = divmod(len(visible), max_lines_per_col)
            draw.text(
                (col_x[col], top_y + row * line_height), f"+{overflow} more",
                font=item_font, fill=self.palette["accent"],
            )

        return item_hits

    def _draw_brightness_buttons(self, draw, card_rect, is_on):
        """Draws "-"/"+" buttons in a light card's bottom-right corner and
        returns their hit rects, keyed "minus"/"plus", for handle_tap()."""
        x0, y0, x1, y1 = card_rect
        size = BRIGHTNESS_BUTTON_SIZE
        pad = 12
        gap = 10
        plus_rect = (x1 - size - pad, y1 - size - pad, x1 - pad, y1 - pad)
        minus_rect = (
            plus_rect[0] - gap - size, plus_rect[1],
            plus_rect[0] - gap, plus_rect[3],
        )
        buttons = {"minus": minus_rect, "plus": plus_rect}
        for label, rect in (("-", minus_rect), ("+", plus_rect)):
            bx0, by0, bx1, by1 = rect
            draw.rounded_rectangle(
                [bx0, by0, bx1, by1], radius=10,
                fill=self.palette["accent"] if is_on else self.palette["bg"],
            )
            tw = draw.textlength(label, font=self.font_state)
            draw.text(
                (bx0 + (size - tw) / 2, by0 + (size - 34) / 2), label, font=self.font_state,
                fill=self.palette["bg"] if is_on else self.palette["text"],
            )
        return buttons

    def _draw_toggle_square(self, draw, x, y, completed):
        color = self.palette["toggle_done"] if completed else self.palette["toggle_pending"]
        draw.rounded_rectangle([x, y + 2, x + TOGGLE_SIZE, y + 2 + TOGGLE_SIZE], radius=3, fill=color)


def _friendly_name(state, entity_id):
    if state:
        name = state.get("attributes", {}).get("friendly_name")
        if name:
            return name
    return entity_id


def _format_state(state, domain=None):
    if not state:
        return "unavailable"
    value = state.get("state", "unknown")
    attributes = state.get("attributes", {})
    if domain == "light" and value == "on" and attributes.get("brightness") is not None:
        return f"on {round(attributes['brightness'] / 255 * 100)}%"
    unit = attributes.get("unit_of_measurement")
    return f"{value} {unit}" if unit else value


def _truncate_text(draw, text, font, max_width):
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…" if text else "…"


def _fit_cover(image, width, height):
    """Scale an image to fully cover a width x height box (upscaling if
    needed) and center-crop the overflow, like CSS 'background-size: cover'.
    """
    src_w, src_h = image.size
    scale = max(width / src_w, height / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    resized = image.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    return resized.crop((left, top, left + width, top + height))


# ---------------------------------------------------------------------------
# Screensaver ("smart frame" idle mode)
# ---------------------------------------------------------------------------

class Screensaver:
    """Full-screen idle display: rotates local photos (if configured) with a
    clock/date overlay, or just the clock/date on a black background if no
    photos are available. Photo selection and rotation are both derived
    purely from the wall clock (current time // photo_interval_s), so the
    render is stateless across activations - no need to track "when did this
    screensaver session start"."""

    def __init__(self, width, height, photo_dir, photo_interval_s):
        self.width = width
        self.height = height
        self.photo_interval_s = max(5, photo_interval_s)
        self.font_clock = _load_font(FONT_BOLD_CANDIDATES + FONT_CANDIDATES, 90)
        self.font_date = _load_font(FONT_CANDIDATES, 32)
        self._photos = self._scan_photos(photo_dir)
        self._cache_index = None
        self._cache_img = None

        if photo_dir and not self._photos:
            log.warning("Screensaver photo dir %s has no usable images; showing clock only", photo_dir)
        elif self._photos:
            log.info("Screensaver: %d photo(s) found in %s", len(self._photos), photo_dir)

    def redraw_key(self, now):
        """A cheap-to-compare value that changes exactly when the rendered
        frame should change (the displayed minute, and/or the photo slot)."""
        photo_slot = int(now // self.photo_interval_s) % len(self._photos) if self._photos else 0
        return (time.strftime("%H:%M"), photo_slot)

    def render(self, now):
        img = Image.new("RGB", (self.width, self.height), (0, 0, 0))
        if self._photos:
            slot = int(now // self.photo_interval_s) % len(self._photos)
            photo = self._load_photo(slot)
            if photo is not None:
                img.paste(photo, (0, 0))
        self._draw_clock(img)
        return img

    def _scan_photos(self, photo_dir):
        if not photo_dir:
            return []
        try:
            names = sorted(
                name for name in os.listdir(photo_dir)
                if name.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".gif"))
            )
        except OSError as exc:
            log.warning("Could not read screensaver photo dir %s: %s", photo_dir, exc)
            return []
        return [os.path.join(photo_dir, name) for name in names]

    def _load_photo(self, index):
        if index == self._cache_index:
            return self._cache_img
        path = self._photos[index]
        try:
            with Image.open(path) as raw:
                fitted = _fit_cover(raw.convert("RGB"), self.width, self.height)
        except Exception:
            log.exception("Failed to load screensaver photo %s", path)
            fitted = None
        self._cache_index, self._cache_img = index, fitted
        return fitted

    def _draw_clock(self, img):
        draw = ImageDraw.Draw(img)
        clock_text = time.strftime("%H:%M")
        date_text = time.strftime(f"%A, %B {int(time.strftime('%d'))}")

        pad = 24
        margin = 32
        line_gap = 8
        clock_w = draw.textlength(clock_text, font=self.font_clock)
        date_w = draw.textlength(date_text, font=self.font_date)
        box_w = max(clock_w, date_w) + pad * 2
        box_h = 90 + 32 + line_gap + pad * 2

        x0, y0 = margin, self.height - margin - box_h
        x1, y1 = x0 + box_w, self.height - margin
        draw.rounded_rectangle([x0, y0, x1, y1], radius=18, fill=(0, 0, 0))
        draw.text((x0 + pad, y0 + pad), clock_text, font=self.font_clock, fill=(255, 255, 255))
        draw.text(
            (x0 + pad, y0 + pad + 90 + line_gap), date_text, font=self.font_date, fill=(220, 220, 220),
        )


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
                    if None not in (state.start_x, state.start_y, state.x, state.y):
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
        for action, rect in card.buttons.items():
            if _point_in_rect(pos, rect):
                step = BRIGHTNESS_STEP_PCT if action == "plus" else -BRIGHTNESS_STEP_PCT
                log.info("Tap at %s: adjusting brightness of %s by %+d%%", pos, card.entity_id, step)
                client.call_service("light", "turn_on", card.entity_id, {"brightness_step_pct": step})
                return
        if _point_in_rect(pos, card.rect):
            if card.domain in TOGGLE_DOMAINS:
                log.info("Tap at %s: toggling %s", pos, card.entity_id)
                client.call_service(card.domain, "toggle", card.entity_id)
            else:
                log.debug("Tap at %s on read-only card: %s", pos, card.entity_id)
            return


def handle_widget_tap(pos, widgets, renderer, client):
    """Returns True if a to-do widget consumed the tap - i.e. the tap landed
    inside a widget's rect at all - False if it should fall through to the
    entity grid instead. Tapping an item's toggle square on an interactive
    widget (chores) flips it between "needs_action" and "completed" in Home
    Assistant. Tapping anywhere else on an expandable widget (the shopping
    list) toggles its full-screen view."""
    for widget in widgets:
        if not _point_in_rect(pos, widget["rect"]):
            continue
        if widget["interactive"]:
            hit = next((h for h in widget["item_hits"] if _point_in_rect(pos, h["rect"])), None)
            if hit:
                new_status = "needs_action" if hit["status"] == "completed" else "completed"
                log.info("Tap at %s: setting %s on %s to %s", pos, hit["uid"], hit["entity_id"], new_status)
                client.set_todo_item_status(hit["entity_id"], hit["uid"], new_status)
        elif widget["expandable"]:
            renderer.expanded = not renderer.expanded
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
# Scheduled display-off ("night mode")
# ---------------------------------------------------------------------------

def _parse_hhmm(value):
    """Parse a "HH:MM" string into minutes-since-midnight, or None if blank
    or malformed (logged, then ignored so a typo can't wedge the display)."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        hh, mm = value.split(":")
        minutes = int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        log.warning("Invalid display schedule time %r (expected HH:MM) - ignoring", value)
        return None
    if 0 <= minutes < 24 * 60:
        return minutes
    log.warning("Display schedule time %r out of range - ignoring", value)
    return None


def _fmt_hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _in_off_window(now_min, off_min, on_min):
    """True if now_min (minutes since local midnight) falls in the [off, on)
    display-off window. Handles windows that wrap past midnight (e.g. off at
    22:00, on at 07:00). Disabled (always False) unless both bounds are set
    and distinct."""
    if off_min is None or on_min is None or off_min == on_min:
        return False
    if off_min < on_min:
        return off_min <= now_min < on_min
    return now_min >= off_min or now_min < on_min


class Backlight:
    """Best-effort control of any /sys/class/backlight devices, used to power
    the panel down during the scheduled display-off window.

    Turning off writes BOTH brightness=0 and bl_power=1: on the original Pi
    7" DSI display's KMS panel driver, bl_power alone often doesn't actually
    cut the backlight, but brightness=0 does; on other drivers it's the
    reverse - so we set both and let whichever the driver honours take
    effect. Each device's brightness is snapshotted at startup and restored
    on wake, so waking doesn't clobber a user's chosen brightness with max.

    Does nothing (harmlessly) when no backlight device is exposed, e.g. an
    HDMI monitor - the black framebuffer frame is the visible fallback there.
    """

    def __init__(self):
        self._devices = []  # (path, saved_brightness_or_None)
        self._dead = set()  # device paths that failed a write (e.g. read-only /sys)
        self._last_error = None
        base = "/sys/class/backlight"
        try:
            names = sorted(os.listdir(base))
        except OSError:
            names = []
        for name in names:
            path = os.path.join(base, name)
            self._devices.append((path, self._read_int(os.path.join(path, "brightness"))))
        if self._devices:
            log.info(
                "Backlight control: found %s",
                ", ".join(f"{os.path.basename(p)} (brightness={b})" for p, b in self._devices),
            )
        else:
            log.info(
                "Backlight control: no /sys/class/backlight device found - "
                "display-off will draw a black screen but cannot dim the backlight"
            )

    @staticmethod
    def _read_int(path):
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def _write(self, path, value):
        try:
            with open(path, "w") as f:
                f.write(str(value))
        except OSError as exc:
            self._last_error = exc

    def set(self, on):
        for path, saved in self._devices:
            if path in self._dead:
                continue
            self._last_error = None
            if on:
                self._write(os.path.join(path, "bl_power"), 0)
                if saved is not None:
                    self._write(os.path.join(path, "brightness"), saved)
            else:
                self._write(os.path.join(path, "brightness"), 0)
                self._write(os.path.join(path, "bl_power"), 1)
            if self._last_error is not None:
                # /sys is read-only in the add-on container, so these writes
                # fail; warn once per device, then stay quiet and let
                # Framebuffer.blank() (FBIOBLANK) do the actual work.
                self._dead.add(path)
                log.warning(
                    "Backlight %s not writable (%s) - relying on FBIOBLANK / black screen instead",
                    os.path.basename(path), self._last_error,
                )


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
    screensaver_timeout_s = int(os.environ.get("SCREENSAVER_TIMEOUT_S", "60") or 0)
    screensaver_photo_dir = os.environ.get("SCREENSAVER_PHOTO_DIR", "").strip()
    screensaver_photo_interval_s = int(os.environ.get("SCREENSAVER_PHOTO_INTERVAL_S", "300") or 300)
    display_off_min = _parse_hhmm(os.environ.get("DISPLAY_OFF_TIME", ""))
    display_on_min = _parse_hhmm(os.environ.get("DISPLAY_ON_TIME", ""))
    if (display_off_min is None) != (display_on_min is None):
        log.warning("Both DISPLAY_OFF_TIME and DISPLAY_ON_TIME must be set for scheduled display-off - disabling it")
        display_off_min = display_on_min = None
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
    if screensaver_timeout_s > 0:
        log.info("Screensaver: activates after %ds idle", screensaver_timeout_s)
        if screensaver_photo_dir:
            log.info("Screensaver photos: reading from %s", screensaver_photo_dir)
    else:
        log.info("Screensaver disabled (screensaver_timeout_s=0)")
    if display_off_min is not None:
        log.info(
            "Scheduled display-off: dark from %s to %s (local time) once idle",
            _fmt_hhmm(display_off_min), _fmt_hhmm(display_on_min),
        )

    stop_event = threading.Event()
    threading.Thread(
        target=memory_watchdog, args=(min_free_memory_mb, stop_event), name="mem-watchdog", daemon=True
    ).start()

    console_fd = hide_console_cursor()
    fb = Framebuffer(os.environ.get("FB_DEVICE", "/dev/fb0"))
    renderer = Renderer(fb.xres, fb.yres, entities, dark_mode, shopping_list_entity, chores_entities)
    touch = TouchInput(fb.xres, fb.yres)
    backlight = Backlight()

    def set_display_power(on):
        # FBIOBLANK on the fb device is the mechanism that actually works in
        # the container (writable fd, no /sys needed); the sysfs backlight
        # write is a best-effort secondary for setups where /sys is writable.
        fb.blank(not on)
        backlight.set(on)

    screensaver = (
        Screensaver(fb.xres, fb.yres, screensaver_photo_dir, screensaver_photo_interval_s)
        if screensaver_timeout_s > 0 else None
    )

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
    last_activity = time.time()
    mode = None  # "dashboard" | "screensaver" | "dark"; None until first draw
    last_screensaver_key = None
    set_display_power(True)  # in case a prior run left the panel blanked
    try:
        while True:
            now = time.time()
            local = time.localtime(now)
            now_min = local.tm_hour * 60 + local.tm_min

            idle = screensaver_timeout_s > 0 and now - last_activity >= screensaver_timeout_s
            night = _in_off_window(now_min, display_off_min, display_on_min)

            # When idle, the display sleeps: goes dark inside the scheduled
            # off-window, otherwise shows the screensaver. Either way a tap
            # wakes it back to the dashboard, and it sleeps again one timeout
            # later - so "dark" is just the night-time flavour of screensaver.
            if idle and night:
                new_mode = "dark"
            elif idle and screensaver:
                new_mode = "screensaver"
            else:
                new_mode = "dashboard"

            if new_mode == "dark":
                if mode != "dark":
                    mode = "dark"
                    fb.blit(Image.new("RGB", (fb.xres, fb.yres), (0, 0, 0)))
                    set_display_power(False)
                dirty.clear()
            elif new_mode == "screensaver":
                if mode != "screensaver":
                    if mode == "dark":
                        set_display_power(True)
                    mode = "screensaver"
                    last_screensaver_key = None
                # HA state updates redraw neither the dashboard nor the
                # screensaver - only the wall clock/photo rotation do, since
                # idle is tracked purely off touch input.
                dirty.clear()
                key = screensaver.redraw_key(now)
                if key != last_screensaver_key:
                    last_screensaver_key = key
                    fb.blit(screensaver.render(now))
            else:  # dashboard
                if mode != "dashboard":
                    if mode == "dark":
                        set_display_power(True)
                    mode = "dashboard"
                    dirty.set()
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
                    last_activity = time.time()
                    if mode in ("dark", "screensaver"):
                        # First tap only wakes the display - it is not also
                        # applied to whatever dashboard element is underneath.
                        # Clearing idle flips the mode back to dashboard next
                        # loop, which re-enables the backlight and redraws.
                        dirty.set()
                    elif handle_widget_tap(tap, last_widgets, renderer, client):
                        dirty.set()
                    else:
                        handle_tap(tap, last_cards, client)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        set_display_power(True)
        fb.close()
        restore_console_cursor(console_fd)


if __name__ == "__main__":
    main()
