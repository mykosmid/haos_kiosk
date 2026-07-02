# HAOS-kiosk

Display HA dashboards in kiosk mode directly on your HAOS server.

## Author: Jeff Kosowsky (version: 1.3.2, April 2026)

## Description

Launches X-Windows on local HAOS server followed by OpenBox window manager
and Luakit browser starting with your configured default Home Assistant
dashboard.

- Standard mouse, touchscreen, and keyboard interactions should work
  automatically as well as audio
- Supports touchscreens, screen rotation, and onscreen keyboard


You can press `ctl-R` at any time to refresh ( reload) the browser. \
Alternatively, you can right click (or long press touchscreen) to access
browser menu that includes options for page `Back`, `Forward`, `Stop`, and
`Reload`.

**NOTE:** You must enter your HA username and password in the
*Configuration* tab for the Add-on to start.

**NOTE:** The Add-on requires a valid, connected display in order to
start.\
If your display does not show up, try rebooting and restarting the Add-on
with the display attached

**NOTE:** Should support any standard mouse, touchscreen, keypad and
touchpad so long as its `/dev/input/eventN` number is less than 25.

**NOTE:** If you encounter issues with the Add-on, please first check the
HAOSKiosk github
[issues page](https://github.com/puterboy/HAOS-kiosk/issues) (open and
closed), then try the testing branch (add the following url to the
repository: https://github.com/puterboy/HAOS-kiosk#testing). If still
please file an
[issue on github](https://github.com/puterboy/HAOS-kiosk/issues) and
\*\*include full details of your setup (including computer hardware and
display type details)and what you did along with a complete log.



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

3. You must enter your HA username and password in the **Configuration**
   tab.

4. Press **Start** to run the Add-on.

**If you are having trouble installing the add-on or getting displays and
touchscreens working, please see the **TROUBLESHOOTING** section below as
well as the github issues page
(https://github.com/puterboy/HAOS-kiosk/issues) as many common issues have
already been addressed and resolved**

______________________________________________________________________

## Configuration Options

### HA Username [required]

Enter your Home Assistant login name.

### HA Password [required]

Enter your Home Assistant password.

### HA URL

Default: `http://localhost:8123`\
In general, you shouldn't need to change this since this is running on the
local server.

### HA Dashboard

Name of starting dashboard.\
(Default: "" - loads the default `Lovelace` dashboard)

### Login Delay

Delay in seconds to allow login page to load.\
(Default: 1 second)

### Zoom Level

Level of zoom with `100` being 100%.\
(Default: 100%)

### Browser Refresh

Time between browser refreshes. Set to `0` to disable.\
Recommended because with the default RPi config, console errors *may*
overwrite the dashboard.\
(Default: 600 seconds)

### Screen Timeout

Time before screen blanks in seconds. Set to `0` to never timeout.
(Default: 0 seconds - never timeout)

### Output Number

Choose which of the *connected* video output ports to use. Set to `1` to
use the first connected port. If selected number exceeds number of
connected ports, then use last valid connected port. (Default: 1)

NOTE: This should always be set to `1` unless you have more than one video
output device connected. If so, use the logs to see how they are numbered.

### Dark Mode

Prefer dark mode where supported if `True`, otherwise prefer light mode.
(Default: True). This preference applies to all URLs

NOTE: This preference applies to all URLs unless overridden in the URL. In
particular, in Home Assistant web pages, This preference for light or dark
mode only takes effect if the user profile (under 'Theme') is set to
`auto`. Otherwise, the user profile `light` or `dark` setting takes
precedence. Similarly, the `Primary` and `Accent` colors set in the profile
take precedence *unless* `HA Theme` is set.

### HA Theme

Set HA theme to given string. This setting applies only to HA dashboards
and may override the value of DARK_MODE unless the theme support both dark
and light variants. See HACS for downloadable themes to use. (Default:
True)

NOTE: You can force the dark or light default theme specifically for HA
dashboards by setting the theme to `{"dark":true}` or `{"dark":false}`
respectively. Similarly, leaving the theme blank (or setting it to `{}` or
`Home Assistant`) is equivalent to "auto", in which case the default light
or dark scheme is governed by the value of DARK_MODE.

### HA Sidebar

Presentation of left sidebar menu (device-specific).\
Options include: (Default: None)

- Full (icons + names)
- Narrow (icons only)
- None (hidden)

### Rotate Display

Rotate the display relative to standard view.\
Options include: (Default: Normal)

- Normal (No rotation)
- Left (Rotate 90 degrees clockwise)
- Right (Rotate 90 degrees counter-clockwise)
- Inverted (Rotate 180 degrees)

### Map Touch Inputs

Map touch inputs to the selected video output, so that the touch devices
get rotated consistently with the video output. (Default: True)

### Cursor Timeout

Time in seconds for cursor to be hidden after last mouse movement or touch.
Cursor will reappear when mouse moved or screen touched again. Set to `0`
to *always* show cursor. Set to `-1` to *never* show cursor. (Default: 5
seconds)

### Keyboard Layout

Set the keyboard layout and language. (Default: us)

### Xorg.conf

Append to or replace existing, default xorg.conf file.\
Select 'Append' or 'Replace options.\
To restore default, set to empty and select 'Append' option.

### Debug

For debugging purposes, launches `Xorg` and `openbox` and then sleeps
without launching `luakit`.\
Manually, launch `luakit` (e.g.,
`luakit -U localhost:8123/<your-dashboard>`) from Docker container.\
E.g., `sudo docker exec -it addon_haoskiosk bash`

### Screensaver Enabled

If `True`, shows a fullscreen image slideshow after the dashboard has been
idle (no touch, mouse, or key input) for **Screensaver Timeout** seconds.
Any input dismisses the slideshow and returns to the dashboard. (Default:
False)

Images are pulled live from Home Assistant's built-in local **Media**
source, so you can upload/replace them remotely at any time from the
Home Assistant mobile app or web UI (sidebar **Media** page), without
touching the kiosk device or restarting the Add-on.

### Screensaver Timeout

Idle time in seconds before the screensaver starts. (Default: 300)

### Screensaver Interval

Time in seconds between images shown in the slideshow. (Default: 15)

### Screensaver Media Folder

Name of the folder (under Home Assistant's local Media source) that holds
the screensaver images, e.g. `screensaver` maps to `/media/screensaver` on
the Home Assistant host. Upload images into this folder via the Media page
in the HA app or web UI — the folder is created automatically the first
time you upload a file into it. (Default: "screensaver")

______________________________________________________________________

## KEYBOARD SHORTCUTS

The following new fixed keyboard shortcuts are defined (but subject to
change).

- **Ctrl+r:** *Reload page*

- **Ctrl+Left:** *Go back in the browser tab history*

- **Ctrl+Right:** *Go forward in the browser tab history*

- **Ctrl+Alt+t:** *Open new tab*

- **Ctrl+Alt+Shift+t:** *Close current tab*

- **Ctrl+Alt+w:** *Open new window*

- **Ctrl+Alt+Shift+w:** *Close current window* (except for last window)

- **Ctl+Alt+Left:** *Previous tab*

- **Ctl+Alt+Right:** *Next tab*

- **Ctl+Alt+Shift+Left:** *Previous window* (Also: **Alt+Shift+Tab**)

- **Ctl+Alt+Shift+Right:** *Next window* (Also: **Alt+Tab**)

Note that the Openbox Window manager defines many other default bindings.

______________________________________________________________________

## MISCELLANEOUS NOTES

#### Luakit browser

The Luakit browser is launched in kiosk-like (*passthrough*) mode. In
general, you want to stay in `passthrough` mode to preserve the kiosk-like
experience and pass all keystrokes to the browser page (except for explicit
bindings as defined above)

Luakit modes and commands are similar to vi

- To enter *normal* mode (similar to command mode in `vi`), press
  `ctl-alt-esc`.

- To return to *passthrough* mode, press `ctl-Z` or alternatively, press
  `i` to enter *insert*

See [luakit documentation](https://wiki.archlinux.org/title/Luakit) for
further usage information and available commands.

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
