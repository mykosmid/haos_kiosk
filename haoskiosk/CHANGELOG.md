# Changelog

## v2.0.0 - July 2026

- **Breaking rewrite**: replaced the entire Xorg + openbox + luakit (WebKit2)
  browser stack with a small native Python app (`kiosk.py`) that renders
  directly to the Linux framebuffer (`/dev/fb0`) and reads touch input
  directly from `/dev/input` via `evdev` - no X server, window manager, or
  browser engine anywhere in the container. This is a large memory reduction
  on constrained devices (e.g. a 1GB Raspberry Pi), at the cost of no longer
  showing the full Lovelace dashboard: the add-on now shows a fixed grid of
  entities you configure (`entities`), with tap-to-toggle for
  `light`/`switch`/`input_boolean`/`fan` domains and read-only display for
  everything else, talking directly to Home Assistant's WebSocket API.
- **Breaking config change**: `ha_username`/`ha_password` are replaced by
  `ha_token` (a Home Assistant long-lived access token), since WebSocket
  authentication requires a token rather than a login form.
- Removed `ha_dashboard`, `login_delay`, `browser_refresh`, `output_number`,
  `map_touch_inputs`, `keyboard_layout`, `rotate_display`, `screen_timeout`,
  and all `screensaver_*` options - these were all specific to the
  browser-based dashboard/screensaver, which no longer exists in this
  version. Display rotation and idle dimming/screensaver may return as
  native features in a future release.
- `dark_mode` and `debug_mode` are kept but repurposed for the native app
  (dashboard color palette, and skip-launch-for-manual-debugging
  respectively); `min_free_memory_mb` keeps its previous meaning, now
  restarting `kiosk.py` instead of `luakit`.
- Set `ingress: false` - the add-on has no web UI to embed, so leaving
  ingress enabled produced a non-functional panel entry.
- Dropped the `SYS_ADMIN` privilege and the `/dev/tty0` remount/delete hack,
  `dbus`, and `udev` - all were specific to getting Xorg to start and are
  unnecessary once there's no X server.

## v1.4.0 - July 2026

- Added built-in screensaver: after a configurable idle timeout, shows a
  fullscreen slideshow of images pulled live from Home Assistant's local
  Media source (`screensaver_enabled`, `screensaver_timeout`,
  `screensaver_interval`, `screensaver_media_folder`). Images can be
  uploaded/replaced remotely at any time via the HA app or web UI's Media
  page - no separate file share or add-on needed. Any touch, mouse, or key
  input dismisses it. Images are decoded and downscaled to screen size via
  `createImageBitmap` (falling back to a plain `<img>` if unsupported) so
  large photo originals can't exhaust memory on constrained devices.
- Added automatic screensaver media sync: a background watcher
  (`screensaver_sync.sh`, requires `imagemagick`/`inotify-tools`) watches
  `screensaver_source_folder` (default: "My media" root) and auto-resizes
  (`screensaver_resize_width`/`screensaver_resize_height`, default
  1920x1080) any images dropped there into `screensaver_media_folder`, so
  original phone photos can be uploaded as-is without manual resizing.
- Removed stale `examples/screensaver.sh`, which relied on a REST API that
  no longer exists in this Add-on.
- Fixed screensaver crash ("Native Windows wider or taller than 32767
  pixels are not supported"): the canvas backing surface was sized to the
  decoded bitmap's actual resolution, which can exceed X11's window/pixmap
  size limit if this WebKit build ignores `createImageBitmap`'s
  `resizeWidth` option (e.g. for wide panorama photos). The canvas is now
  always capped to the computed target size regardless of bitmap size.
- Added low-memory watchdog (`min_free_memory_mb`, default 100): on
  memory-constrained devices (e.g. a 1GB Raspberry Pi), periodically
  checks available system memory and proactively restarts luakit before
  the kernel OOM-killer can kill a WebKit process mid-render, which
  otherwise tends to freeze the display (screen on, page/HA connection
  dead) rather than recovering on its own. Set to `0` to disable.

## v1.3.2 - April 2026

- Added explicit BUILD_FROM location to Dockerfile for ha core 2026.04+

## v1.3.1 - April 2026

- Updated auto-login JS injection in 'userconf.lua' for 2026.4+
- Fixed whitelist logic to allow commands outside of default path

## v1.3.0 - February 2026

- Added more key bindings for opening/closing/rotating tabs and windows
- Add x11vnc server to facilitate remote viewing or debugging of kiosk
- Added 'screenshot' function to REST_API and gesture action commands
- Added `enable_inputs` and `disable_inputs` functions to REST_API to allow
  locking down (and unlocking) inputs by disabling keyboard, mouse and
  touch functions
- Added `mute_audio`, `unmute_audio` and `toggle_audio` functions to
  REST_API to change audio state (`toggle_audio` can also be used in
  gesture action commands)
- Converted default gestures in `config.yaml` to use internal
  `kiosk.<function>` handlers rather than calling shell functions
- Added short list of built-in keyboard shortcuts
- Revamped `ultrasonic-trigger.py` example and added new functionality to
  enable/disable inputs, mute/unmute audio, and rotate through a list of
  URLs
- Added INSTRUCTIONS section to README.md (thanks: @cvroque)
- Added more details to README.

## v1.2.0 - January 2026

- Added ability to set HA theme in config.yaml
- Added USB audio (`audio: true` and `usb: true` in config.yaml) Added
  corresponding config option `audio_sink` which can be: auto, hdmi, usb,
  or none.
- Increased ulimit (in config.yaml) to reduce crashes from heavy usage
- Improved browser refresh logic and stability by:
  - Changing browser refresh from JS injection to native luakit view:reload
  - Forcing hard reload (including cache) every HARD_RELOAD_FREQ reloads
    (refreshes)
  - Killing and restarting luakit if ang page fails to reload more than
    MAX_LOAD_FAILURES in a row
- Improved logging of browser refresh
- Added luakit memory process logging after every page load
- Added JS injections to protect against browser errors & crashes
- Improved robustness and debug output for associating udevadm paths with
  libinput list devices
- Changed run.sh exit logic so that quits if no luakit process for at least
  10 seconds (even if original luakit process has exited)
- Removed config.yaml parameter `allow_user_command` and replaced with
  `command_whitelist` regex. Also added internal whitelist, blacklist, and
  dangerous shell tokens list along with path restrictions (see README.md)
  for details on how behavior has changed.
- Wrote complete Python 'xinput2' parser to detect broad range of mouse and
  touch gestures and execute gesture-specific commands. Replaces prior very
  limited tkinter implementation. See 'mouse_touch_inputs.py' and
  'gesture_commmands.json'
- Added corresponding 'gestures' list option to config.yaml
- Added 'Option "GrabDevice" "true"' to keyboard InputClass section in
  xorg.conf
- Added mouse buttons (left/right/middle/drag) to default Onboard keyboard
  layout
- Refactored and rewrote `rest_server.py`
- Added `REST_IP` to options to allow users to set the listening IP address
- Changed onscreen_keyboard option default to `true`
- README edits

## v1.1.1 - September 2025

- Auto-detect drm video card used and set 'kmsdev' accordingly in xorg.conf
- Added more system & display logging
- Minor bug fixes and tweaks

## v1.1.0 - September 2025

- Added REST API to allow remote launching of new urls, display on/off,
  browser refresh, and execution of one or more shell commands
- Added onscreen keyboard for touch screens (Thanks GuntherSchulz01)
- Added 'toogle_keyboard.py' to create 1x1 pixel at extreme top-right to
  toggle keyboard visibility
- Save DBUS_SESSION_BUS_ADDRESS to ~/.profile for use in other (login)
  shells
- Code now potentially supports xfwm4 window manager as well as Openbox
  (but xfwm4 commented out for now)
- Revamped 'Xorg.conf.default' to use more modern & generalized structure
- Prevent luakit from automatically restoring old sessions
- Patched luakit unique_instance.lua to open remote url's in existing tab
- Force (modified) passthrough mode in luakit with every page load to
  maximize kiosk-like behavior and hide potentially conflicting command
  mode
- Removed auto refresh on display wake (not necessary)

## v1.0.1 - August 2025

- Simplified and generalzed libinput discovery tagging and merged resulting
  code into 'run.sh' (Thanks to GuntherSchulz01 and tacher4000)
- Added "CURSOR_TIMEOUT" to hide cursor (Thanks tacher4000)
- Set LANG consistent with keyboard layout (Thanks tacher4000)
- Added additional logging to help debug any future screen or input (touch
  or mouse) issues
- Substituted luakit browser-level Dark Mode preference for HA-specific
  theme preference (Thanks tacher4000)

## v1.0.0 - July 2025

- Switched from (legacy) framebuffer-based video (fbdev) to OpenGL/DRI
  video
- Switched from (legacy) evdev input handling to libinput input handling
- Switched from "HDMI PORT" to "OUTPUT NUMBER" to determine which physical
  port is displayed
- Added 'rotation' config to rotate display
- Added boolean config to determine whether touch inputs are mapped to the
  display output (in particular, this will rotate them in sync)
- Modified 'xorg.conf' for consistency with 'OpenGL/DRI' and 'libinput'
- Attempted to maximize compatibility across RPi and x86
- Added ability to append to or replace default 'xorg.conf'
- Added ability to set keyboard layout. (default: 'us')
- Updated & improved userconf.lua code
- Extensive changes and improvements to 'run.sh' code
- Added back (local) DBUS to allow for inter-process luakit communication
  (e.g., to allow use of unique instance)

## v0.9.9 - July 2025

- Removed remounting of /dev/ ro (which caused HAOS updates to fail)
- Added 'debug' config that stops add-on before launching luakit
- Cleaned up/improved code in run.sh and userconf.lua
- Reverted to luakit=2.3.6-r0 since luakit=2.4.0-r0 crashes (temporary fix)

## v0.9.8 – June 2025

- Added ability to set browser theme and sidebar behavior
- Added <Control-r> binding to reload browser screen
- Reload browser screen automatically when returning from screen blank
- Improved input validation and error handling
- Removed host dbus dependency
- Added: ingress: true
- Tightened up code
- Updated documentation

## v0.9.7 – April 2025

- Initial public release
- Added Zoom capability

## 0.9.6 – March 2025

- Initial private release
