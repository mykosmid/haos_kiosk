# HAOS-kiosk

Display a small, native Home Assistant entity dashboard directly on your HAOS
server's touchscreen - no browser involved.

## Description

Renders a compact grid of entity cards straight to the Linux framebuffer
using a small Python app, and reads touchscreen input directly from
`/dev/input`. There is no X server, no window manager, and no browser engine
running at all - just enough software to show a handful of entities and let
you tap to toggle them. This keeps memory usage far lower than a full
Home-Assistant-in-a-browser kiosk, which matters a lot on memory-constrained
devices like a 1GB Raspberry Pi.

This is a deliberately minimal display: a fixed grid of entities you choose,
each showing its name and current state. `light`, `switch`, `input_boolean`,
and `fan` entities can be tapped to toggle; everything else (sensors, binary
sensors, etc.) is shown read-only. There is no Lovelace, no dashboard editor,
no graphs, and no scrolling - if you need any of that, this add-on isn't the
right fit.

A read-only shopping list widget can also be shown in the bottom-right
quarter of the screen, pulled live from a Home Assistant `todo` list (e.g.
the built-in Shopping List integration). It is enabled by default and shown
regardless of how many entities you configure. Tap it to expand it to a
full-screen view; tap anywhere to collapse it back.

An optional chores widget can also be shown in the top-right quarter of the
screen, combining one or more other Home Assistant `todo` lists (e.g. a
shared list plus a roommate's own list). Unlike the shopping list widget,
each item shows a square toggle button instead of a bullet - red while
pending, green once completed - and tapping it flips that item's status
directly in Home Assistant; it doesn't have a full-screen view since its box
is already sized to fit the whole list. It's disabled by default since
there's no built-in chores list entity to point it at.

If only one of the two widgets is enabled, it takes up the entire right half
of the screen instead of just its quarter, since there's no other widget to
share that space with. The entity grid always fills the remaining space (the
left half, whenever a widget is shown; the full screen otherwise).

**NOTE:** You must generate a Home Assistant long-lived access token and
enter it, along with the entities you want to display, in the
*Configuration* tab for the Add-on to start.

**NOTE:** The Add-on requires a valid, connected display in order to start.
If your display does not show up, try rebooting and restarting the Add-on
with the display attached.

**NOTE:** Display rotation is not supported in this version - the display is
expected to be in its normal landscape orientation. An optional
idle/screensaver mode (see **Screensaver Timeout** below) can turn the
display into a smart photo frame after a period of no touch input.

**NOTE:** If you encounter issues with the Add-on, please first check the
HAOSKiosk github
[issues page](https://github.com/puterboy/HAOS-kiosk/issues) (open and
closed). If still please file an
[issue on github](https://github.com/puterboy/HAOS-kiosk/issues) and
\*\*include full details of your setup (including computer hardware and
display type details) and what you did along with a complete log.

### If you appreciate my efforts:

[![Buy Me a Coffee](https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png)](https://www.buymeacoffee.com/puterboy)

______________________________________________________________________

## Installation

1. Click the **ADD ADD-ON REPOSITORY** button below.

   [![Open your Home Assistant instance and show the add Add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fputerboy%2FHAOS-kiosk)

   - Click **Add → Close** (You might need to enter the **internal IP
     address** of your Home Assistant instance first) *or* go to the
     **Add-on store**.
   - Click **⋮ → Repositories**
   - Fill in `https://github.com/puterboy/HAOS-kiosk`
   - Click **Add → Close**

2. Click on the Add-on, press **Install** and wait until the Add-on is
   installed.

3. Generate a long-lived access token: in the Home Assistant web UI, open
   your user profile (click your name in the bottom-left sidebar), scroll
   down to **Long-Lived Access Tokens**, and click **Create Token**. Copy it
   somewhere safe - you won't be able to see it again.

4. In the Add-on's **Configuration** tab, paste that token into **HA
   Long-Lived Access Token** and list the entities you want to see in
   **Entities**.

5. Press **Start** to run the Add-on.

______________________________________________________________________

## Configuration Options

### HA Long-Lived Access Token [required]

Long-lived access token for Home Assistant, generated from your user
profile's "Long-Lived Access Tokens" section.

### HA URL

Default: `http://localhost:8123`\
In general, you shouldn't need to change this since this is running on the
local server.

### Entities [required]

Comma-separated list of entity_ids to display, e.g.
`light.living_room,switch.fan,sensor.temperature`. Shown in the order
listed. `light`, `switch`, `input_boolean`, and `fan` entities can be tapped
to toggle; anything else is shown read-only.

### Shopping List Entity

Entity ID of a Home Assistant `todo` list to show as a read-only widget in
the bottom-right quarter of the display, e.g. `todo.shopping_list` (the
built-in Shopping List integration's default entity). Only items not yet
marked complete are shown. Tap the widget to expand it full-screen; tap
anywhere to collapse it back. Set to `none` to hide the widget.
(Default: `todo.shopping_list`)

### Chores Entities

Comma-separated list of Home Assistant `todo` list entity_ids to combine
into a tap-to-toggle widget in the top-right quarter of the display, e.g.
`todo.chores,todo.roommate_chores`. Each item shows a square toggle button -
red while pending, green once completed - and tapping it calls Home
Assistant's `todo.update_item` service to flip that item's status. Leave
blank to disable - there's no built-in entity for this, so it's off by
default. (Default: disabled)

### Dark Mode

Use a dark color scheme for the dashboard if `True`, otherwise light.
(Default: True)

### Debug

For debugging purposes, sleeps without launching the kiosk display.
Manually launch it (e.g., `python3 /kiosk.py`) from Docker container.\
E.g., `sudo docker exec -it addon_haoskiosk bash`

### Screensaver Timeout (seconds)

Turns the display into a smart-frame screensaver - a full-screen clock and
date, plus rotating photos if a **Screensaver Photo Directory** is
configured - after this many seconds pass with no touch input. Only touches
reset this timer or wake the display; Home Assistant entity state changes do
not. The first tap after the screensaver activates only wakes the display
back to the normal dashboard - it isn't also applied to whatever card or
widget happens to be underneath. Set to `0` to disable. (Default: 60)

### Screensaver Source Directory

Absolute path to a folder of full-resolution photos to pull from, e.g.
`/media/family_photos` (Home Assistant's `/media` folder, mapped
read-write into this Add-on - drop files in there via the Media or File
Editor Add-on/Samba share). Searched recursively.

If set, the Add-on automatically mirrors every photo into **Screensaver
Photo Directory**, downscaled and center-cropped to exactly fill the
display's resolution, so the screensaver never has to decode a
full-resolution photo at render time. It keeps that folder in sync on a
timer (**Screensaver Sync Interval**) - add or remove a photo in the source
folder and it's reflected there automatically, no restart needed. This
runs once synchronously at startup before the display comes up, so a large
source folder will add to Add-on startup time the first time (subsequent
syncs only process what changed).

Leave blank to manage **Screensaver Photo Directory**'s contents yourself
instead - the Add-on won't touch that folder. (Default: blank)

### Screensaver Photo Directory

Absolute path to the folder of photos (`.jpg`, `.jpeg`, `.png`, `.bmp`,
`.gif`) the screensaver actually rotates through full-screen.

- If **Screensaver Source Directory** is set, this folder is fully managed
  by the Add-on (downscaled copies written in, stale ones removed
  automatically) - don't add or edit files in it directly, they'll be
  removed on the next sync.
- If **Screensaver Source Directory** is blank, populate this folder
  yourself; photos aren't resized in that case, so pre-size them to the
  display's resolution for best results and lowest memory use.

Leave both settings blank to show just the clock/date on a black
background instead. (Default: `/media/screensavers`)

### Screensaver Photo Interval (seconds)

How long each photo is shown before rotating to the next, while the
screensaver is active. Only relevant if Screensaver Photo Directory has
photos in it. (Default: 300)

### Screensaver Sync Interval (seconds)

How often to re-scan Screensaver Source Directory for added, removed, or
changed photos and re-sync Screensaver Photo Directory. Only relevant if
Screensaver Source Directory is configured. (Default: 600)

### Minimum Free Memory (MB)

On memory-constrained devices (e.g. a 1GB Raspberry Pi), the kernel's
OOM-killer can silently kill the display process under memory pressure,
leaving the kiosk frozen rather than recovering on its own.

To avoid this, the Add-on checks available system memory every 20 seconds
and proactively restarts the display (a clean, few-second restart) if it
drops below this threshold. Set to `0` to disable. (Default: 100)

______________________________________________________________________

## MISCELLANEOUS NOTES

This add-on intentionally has no browser, no window manager, and no
Lovelace frontend anywhere in its stack. If you need the full Home
Assistant dashboard experience (multiple views, graphs, a dashboard editor,
etc.) on a kiosk display, look at a browser-based kiosk add-on instead -
this project trades that flexibility for a much smaller memory footprint.

______________________________________________________________________

## TROUBLESHOOTING

- If the display is not working on an RPi3, try adding the following lines
  to the `[pi3]` section of your `config.txt` on the boot partition:

  ```
  dtoverlay=vc4-fkms-v3d
  max_framebuffers=2
  ```

- If you see black borders (underscan) around the display on a Raspberry Pi
  you can disable overscan in `config.txt` on the boot partition:

  ```
  disable_overscan=1
  ```
