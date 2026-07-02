--[=[
Add-on: HAOS Kiosk Display (haoskiosk)
File: userconf.lua for HA minimal browser run on server
Version: 1.3.2
Copyright Jeff Kosowsky
Date: April 2026

Code does the following:
    - Sets browser window to fullscreen
    - Sets zooms level to value of $ZOOM_LEVEL (default 100%)
    - Sets new tab and window default to blank page (about:blank)
    - Loads every URL in 'passthrough' mode so that you can type text as needed without triggering browser commands
    - Auto login to Home Assistant using $HA_USERNAME and $HA_PASSWORD
    - Redefines key to return to normal mode (used for commands) from 'passthrough' mode to: 'Ctl+Alt+Esc'
      (rather than just 'Esc') to prevent unintended  returns to normal mode and activation of unwanted commands
    - Adds <Ctrl-r> binding to reload browser screen (all modes)
    - Adds <Ctrl-Left> and <Ctrl-Right> bindings, to move backwards and forwards respectively in the browser history
    - Adds <Ctrl-Alt-Left> and <Ctrl-Alt-Right> bindings to move to previous and next tabs respectively
    - Note <Ctrl-Alt-Shift-Left> and <Ctrl-Alt-Shift-Right> bindings move to previous and next windows (but defined in Openbox window manager bindings, not here
    - Adds <Ctrl-Alt-t> and <Ctrl-Alt-Shift-t> for new and close tab respectively
    - Adds <Ctrl-Alt-w> for new and close window
    - Prevent printing of '--PASS THROUGH--' status line when in 'passthrough' mode
    - Set up periodic browser refresh every $BROWSWER_REFRESH seconds (disabled if 0)
      NOTE: Original method injected JS to refresh page, now using native luakit view:reload command for more robustness
            Also, every HARD_RELOAD_FREQ refreshes, we also fully refresh the cash
      NOTE: this is important since console messages overwrite dashboards
    - Kill current luakit and restart if any page fails to reload MAX_LOAD_FAILURES in a row
    - Allows for configurable browser $ZOOM_LEVEL
    - Set theme based on $HA_THEME.
      If no theme is set (or if set to '{}' or 'Home Assistant') then the default theme is used with light or dark depending on the value of $DARK_MODE
      Similarly, if the theme has both light and dark modes, then the value of $DARK_MODE determines the underlying mode.
      If theme is set to '{"dark":true} or {"dark":false} then the default theme is dark or light respectively, regardless of the value of $DARK_MODE
    - Set Home Assistant sidebar visibility using $HA_SIDEBAR environment variables
    - Set 'browser_mod-browser-id' to fixed value 'haos_kiosk'
    - If using onscreen keyboard, hide keyboard after page (re)load
    - Prevent session restore by overloading 'session.restore
]=]

-- -----------------------------------------------------------------------
-- Load required Luakit modules
local window = require "window"
local webview = require "webview"
local settings = require "settings"
local modes = package.loaded["modes"]

-- -----------------------------------------------------------------------
-- Configurable variables
local new_escape_key = "<Control-Mod1-Escape>" -- Ctl+Alt+Esc
local HARD_RELOAD_FREQ = 10  -- Frequency of fully reloading cache when refreshing page
local MAX_LOAD_FAILURES = 5  -- Maximum number of consecutive page (re)load failures per view before restarting luakit


-- Load in environment variables to configure options
local defaults = {
    HA_USERNAME = "",
    HA_PASSWORD = "",
    HA_URL = "http://localhost:8123",
    DARK_MODE = true,
    HA_SIDEBAR = "",
    HA_THEME = "",

    LOGIN_DELAY = 1,
    ZOOM_LEVEL = 100,
    BROWSER_REFRESH = 600,

    SCREENSAVER_ENABLED = false,
    SCREENSAVER_TIMEOUT = 300,
    SCREENSAVER_INTERVAL = 15,
    SCREENSAVER_MEDIA_FOLDER = "screensaver",  -- Empty means the root of Home Assistant's Local Media source
                        }
local username = os.getenv("HA_USERNAME") or defaults.HA_USERNAME
local password = os.getenv("HA_PASSWORD") or defaults.HA_PASSWORD

local ha_url = os.getenv("HA_URL") or defaults.HA_URL  -- Starting URL
if not ha_url:match("^https?://[%w%.%-%%:]+[/%?%#]?[/%w%.%-%?%#%=%%]*$") then
    msg.warn("Invalid HA_URL value: '%s'; defaulting to %s", os.getenv("HA_URL") or "", defaults.HA_URL)
    ha_url = defaults.HA_URL
end
ha_url = string.gsub(ha_url, "/+$", "") -- Strip trailing '/'
local ha_url_base = ha_url:match("^(https?://[%w%.%-%%:]+)") or ha_url
ha_url_base = string.gsub(ha_url_base, "/+$", "") -- Strip trailing '/'

local raw_dark_mode = os.getenv("DARK_MODE")
if raw_dark_mode == nil then
    dark_mode = defaults.DARK_MODE
else
    dark_mode = raw_dark_mode:lower()
    if dark_mode == "true" then
        dark_mode = true
    elseif dark_mode == "false" then
        dark_mode = false
    else
       dark_mode = defaults.DARK_MODE
    end
end

local raw_sidebar = os.getenv("HA_SIDEBAR") or defaults.HA_SIDEBAR -- Valid entries: full (or ""), narrow, none,
local valid_sidebars = {
    full = '',
    none = '"always_hidden"',
    narrow = '"auto"',
    [""] = ''
}
local sidebar = valid_sidebars[(raw_sidebar or ""):lower()] or ''
if sidebar == '' and raw_sidebar ~= "" and raw_sidebar ~= defaults.HA_SIDEBAR then
    msg.warn("Invalid HA_SIDEBAR value: '%s'; defaulting to unset", raw_sidebar)
    sidebar = ''
end

local theme = os.getenv("HA_THEME") or "" -- Any installed theme name (e.g., "midnight", "google", "minimal"), or empty to not override
if theme ~= "" then
   local firstchar = theme:sub(1,1)
   if firstchar ~= '"' and firstchar ~= "'" and firstchar ~= '{' then
       theme = '"' .. theme .. '"' -- Wrap in quotes
   end
    msg.info("Forcing HA_THEME to: %s", theme)
end

local login_delay = tonumber(os.getenv("LOGIN_DELAY")) or defaults.LOGIN_DELAY -- Delay in seconds before auto-login
if login_delay <= 0 then
    msg.warn("Invalid LOGIN_DELAY value: '%s'; defaulting to %d", os.getenv("LOGIN_DELAY") or "", defaults.LOGIN_DELAY)
    login_delay = defaults.LOGIN_DELAY
end

local zoom_level = tonumber(os.getenv("ZOOM_LEVEL")) or defaults.ZOOM_LEVEL
if zoom_level <= 0 then
    msg.warn("Invalid ZOOM_LEVEL value: '%s'; defaulting to %d", os.getenv("ZOOM_LEVEL") or "", defaults.ZOOM_LEVEL)
    zoom_level = defaults.ZOOM_LEVEL
end

local browser_refresh = tonumber(os.getenv("BROWSER_REFRESH")) or defaults.BROWSER_REFRESH  -- Refresh interval in seconds
if browser_refresh < 0 then
    msg.warn("Invalid BROWSER_REFRESH value: '%s'; defaulting to %d", os.getenv("BROWSER_REFRESH") or "", defaults.BROWSER_REFRESH)
    browser_refresh = defaults.BROWSER_REFRESH
end

msg.info("USERNAME=%s; URL=%s; DARK_MODE=%s; SIDEBAR=%s; THEME=%s; LOGIN_DELAY=%.1f, ZOOM_LEVEL=%d, BROWSER_REFRESH=%d",
    username, ha_url, tostring(dark_mode), sidebar, theme, login_delay, zoom_level, browser_refresh)

local raw_screensaver_enabled = os.getenv("SCREENSAVER_ENABLED")
if raw_screensaver_enabled == nil then
    screensaver_enabled = defaults.SCREENSAVER_ENABLED
else
    screensaver_enabled = raw_screensaver_enabled:lower()
    if screensaver_enabled == "true" then
        screensaver_enabled = true
    elseif screensaver_enabled == "false" then
        screensaver_enabled = false
    else
        screensaver_enabled = defaults.SCREENSAVER_ENABLED
    end
end

local screensaver_timeout = tonumber(os.getenv("SCREENSAVER_TIMEOUT")) or defaults.SCREENSAVER_TIMEOUT  -- Idle seconds before screensaver starts
if screensaver_timeout <= 0 then
    msg.warn("Invalid SCREENSAVER_TIMEOUT value: '%s'; defaulting to %d", os.getenv("SCREENSAVER_TIMEOUT") or "", defaults.SCREENSAVER_TIMEOUT)
    screensaver_timeout = defaults.SCREENSAVER_TIMEOUT
end

local screensaver_interval = tonumber(os.getenv("SCREENSAVER_INTERVAL")) or defaults.SCREENSAVER_INTERVAL  -- Seconds between slideshow images
if screensaver_interval <= 0 then
    msg.warn("Invalid SCREENSAVER_INTERVAL value: '%s'; defaulting to %d", os.getenv("SCREENSAVER_INTERVAL") or "", defaults.SCREENSAVER_INTERVAL)
    screensaver_interval = defaults.SCREENSAVER_INTERVAL
end

-- Path (relative to Local Media root) holding screensaver images; empty means the Local Media root itself
local screensaver_media_folder = os.getenv("SCREENSAVER_MEDIA_FOLDER") or defaults.SCREENSAVER_MEDIA_FOLDER
screensaver_media_folder = string.gsub(screensaver_media_folder, "^/+", ""):gsub("/+$", "") -- Strip leading/trailing '/'

msg.info("SCREENSAVER_ENABLED=%s; SCREENSAVER_TIMEOUT=%d; SCREENSAVER_INTERVAL=%d; SCREENSAVER_MEDIA_FOLDER=%s",
    tostring(screensaver_enabled), screensaver_timeout, screensaver_interval, screensaver_media_folder)

-- -----------------------------------------------------------------------
-- Forward console messages to stdout
settings.set_setting("webview.enable_write_console_messages_to_stdout", true)

-- Prefer Dark mode if set to true
settings.application.prefer_dark_mode = dark_mode

-- Set window to fullscreen
window.add_signal("init", function(w)
    w.win.fullscreen = true
end)

-- Set zoom level for windows (default 100%)
settings.webview.zoom_level = zoom_level

-- Disable smooth scrolling: WEBKIT_DISABLE_COMPOSITING_MODE forces CPU-only
-- rendering (required on RPi4 + DSI touchscreen to avoid a GPU driver hang -
-- see Dockerfile), so scroll animation is extra CPU work with no GPU to
-- offload it to; disabling it removes that cost.
settings.webview.enable_smooth_scrolling = false

-- Set default new tab and window to blank page, rather than commercial luakit page
settings.window.home_page    = "about:blank"
settings.window.new_tab_page = "about:blank"

-- Prevent session restore by overloading 'session.restore'
local session = require "session"
session.restore = function()
    return nil
end

-- -----------------------------------------------------------------------
-- Helper functions
local function single_quote_escape(str) -- Single quote strings before injection into JS
    if not str or str == "" then return str end
    str = str:gsub("\\", "\\\\")
    str = str:gsub("'", "\\'")
    str = str:gsub("\n", "\\n")
    str = str:gsub("\r", "\\r")
    return str
end

-- -----------------------------------------------------------------------
-- Per-view weak table to track last URL for refresh debugging/reset detection
local consecutive_load_failures = setmetatable({}, { __mode = "k" })  -- Weak keys per view to count consecutive reload failures

-- -----------------------------------------------------------------------

-- Auto-login to homeassistant (if on HA url) and set 'sidebar settings

local ha_settings_applied = setmetatable({}, { __mode = "k" }) -- Flag to track if HA settings have already been applied in this session

webview.add_signal("init", function(view)
    ha_settings_applied[view] = false  -- Set theme and sidebar settings once  per view

    -- Reduce CPU-compositing workload (WEBKIT_DISABLE_COMPOSITING_MODE forces
    -- software rendering - see Dockerfile) by neutering CSS animations and
    -- transitions, including inside Shadow DOM (HA's Lovelace cards, dialogs
    -- and sidebar are almost all Shadow DOM custom elements, so a plain
    -- <style> in the document head alone would miss most of them).
    -- Durations are set near-zero rather than exactly 0 so that code waiting
    -- on 'transitionend'/'animationend' events (e.g., dialog close handlers)
    -- still fires.
    local js_disable_animations = [[
        (function() {
            var STYLE_ID = 'haoskiosk-no-animations';
            var css = '*, *::before, *::after {' +
                'animation-duration: 0.001s !important;' +
                'animation-delay: -0.001s !important;' +
                'transition-duration: 0.001s !important;' +
                'transition-delay: -0.001s !important;' +
                'scroll-behavior: auto !important;' +
            '}';

            function injectInto(root) {
                if (!root || (root.querySelector && root.querySelector('#' + STYLE_ID))) return;
                var style = document.createElement('style');
                style.id = STYLE_ID;
                style.textContent = css;
                root.appendChild(style);
            }

            if (document.head) injectInto(document.head);

            (function walk(node) {
                if (node.shadowRoot) {
                    injectInto(node.shadowRoot);
                    node.shadowRoot.querySelectorAll('*').forEach(walk);
                }
                if (node.children) {
                    for (var i = 0; i < node.children.length; i++) walk(node.children[i]);
                }
            })(document.documentElement);

            // Patch attachShadow once so future shadow roots (new cards,
            // dialogs, popups opened later) get the stylesheet too
            if (!Element.prototype.__haoskiosk_attachShadow_patched) {
                var origAttachShadow = Element.prototype.attachShadow;
                Element.prototype.attachShadow = function(init) {
                    var root = origAttachShadow.call(this, init);
                    try { injectInto(root); } catch (e) {}
                    return root;
                };
                Element.prototype.__haoskiosk_attachShadow_patched = true;
            }
        })();
    ]]

    -- Listen for page load status events
    view:add_signal("load-status", function(v, status)  -- Note do NOT used "load-finished" since doesn't handle redirects properly

        -- Inject as early as possible (before HA's custom elements start attaching shadow roots)
        if status == "committed" then
            v:eval_js(js_disable_animations, { source = "disable_animations_early.js", no_return = true })
        end

        -- Restart luakit if consecutive_load_failures > MAX_LOAD_FAILURES
        if status == "failed" then
            consecutive_load_failures[v] = (consecutive_load_failures[v] or 0) + 1

            if consecutive_load_failures[v] < MAX_LOAD_FAILURES then
                msg.warn("Page load failed (%d/%d): %s", consecutive_load_failures[v], MAX_LOAD_FAILURES, v.uri or "unknown")
             else
                local ffi = require("ffi")
                ffi.cdef("int getpid(void);")
                local luakit_pid = ffi.C.getpid()

                local url = v.uri or ha_url
                msg.error("RESTARTING Luakit (PID=%d) after %d page load failures: %s", luakit_pid, MAX_LOAD_FAILURES, url)
                -- Send kill signal to current luakit pid, wait to complete kill, wait for dbus to fully disconnect, remove /tmp ipc file, launch new luakit, echo PID
                local cmd = string.format([[
                  (kill %d;
                   while kill -0 %d 2>/dev/null; do sleep 0.1; done;
                   sleep 2;
                   rm -f /tmp/luakit-%d-* 2>/dev/null;
                   luakit '%s' &
                   echo "New Luakit PID=$!") &
                ]], luakit_pid, luakit_pid, luakit_pid, url)
                os.execute(cmd)
            end

        elseif status ~= "finished" then return end  -- Only proceed when the page is fully loaded
            consecutive_load_failures[v] = 0  -- Reset consecutive load failures counter

         -- Print RSS  memory consumption
        local mem_file = io.open("/proc/self/statm", "r")
        local rss_mb = "NA"
        if mem_file then
            local rss_pages = tonumber(mem_file:read("*a"):match("%S+%s+(%S+)"))
            mem_file:close()
            if rss_pages then
                rss_mb = math.floor(rss_pages * 4 / 1024)  -- Approximate MB (page size ~4kB on most systems)
            end
        end
        msg.info("URL: %s (RSS: %s MB)", v.uri, rss_mb) -- DEBUG

        -- Force passthrough mode on every page load so don't inadvertently type commands in kiosk
        webview.window(v):set_mode("passthrough")

        -- Set up auto-login for Home Assistant
        -- Check if current URL matches the Home Assistant auth page
        if v.uri:match("^" .. ha_url_base .. "/auth/authorize%?response_type=code") then
            msg.info("Authorizing: %s", v.uri) -- DEBUG
            -- JavaScript to auto-fill and submit the login form
            local js_auto_login = string.format([[
                setTimeout(function() {
                    try {
                        // 2026.4+ working version uses shadowRoot; preserve backward-compatibility
                        const haInputs = document.querySelectorAll('ha-input');
                        const usernameField = haInputs[0]?.shadowRoot?.querySelector('wa-input')?.shadowRoot?.querySelector('input[autocomplete="username"]')
                            || document.querySelector('input[autocomplete="username"]');
                        const passwordField = haInputs[1]?.shadowRoot?.querySelector('wa-input')?.shadowRoot?.querySelector('input[autocomplete="current-password"]')
                            || document.querySelector('input[autocomplete="current-password"]');
                        const haCheckbox = document.querySelector('ha-checkbox');
                        const submitButton = document.querySelector('ha-button');

                        if (usernameField && passwordField) {  // Note post 2026.4 requires 'change' event oo
                            usernameField.value = '%s';
                            usernameField.dispatchEvent(new Event('input', { bubbles: true }));
                            usernameField.dispatchEvent(new Event('change', { bubbles: true }));

                            passwordField.value = '%s';
                            passwordField.dispatchEvent(new Event('input', { bubbles: true }));
                            passwordField.dispatchEvent(new Event('change', { bubbles: true }));

                            console.log('Auto-login: fields filled + events dispatched');
                        } else {
                            console.log('Auto-login failed: missing elements', {
                                username: !!usernameField,
                                password: !!passwordField,
                                submit: !!submitButton
                            });
                        }

                        if (haCheckbox) {
                            haCheckbox.setAttribute('checked', '');
                            haCheckbox.dispatchEvent(new Event('change', { bubbles: true }));
                        }

                        if (submitButton) submitButton.click();
                    } catch(e) { console.warn('Auto-login JS error:', e); }
                }, %d);
            ]], single_quote_escape(username), single_quote_escape(password), login_delay * 1000)

            msg.info("Logging in: (username: %s): %s", username, v.uri) -- DEBUG
            v:eval_js(js_auto_login, { source = "auto_login.js", no_return = true })  -- Execute the login script
        end

        -- Set Home Assistant theme and sidebar visibility after dashboard load
        if not ha_settings_applied[v] -- Check if not set yet and current URL starts with ha_url but not an auth page
           and (v.uri .. "/"):match("^" .. ha_url_base .. "/") -- Note ha_url was stripped of trailing slashes
           and not v.uri:match("^" .. ha_url_base .. "/auth/") then

            local js_settings = string.format([[
                try {
                    // Set browser_mod browser ID to "haos_kiosk"
                    localStorage.setItem('browser_mod-browser-id', 'haos_kiosk');

                    // Set sidebar visibility
                    const sidebar = '%s';
                    const currentSidebar = localStorage.getItem('dockedSidebar') || '';

                    if (sidebar !== currentSidebar) {
                        if (sidebar !== "") {
                            localStorage.setItem('dockedSidebar', sidebar);
                        } else {
                            localStorage.removeItem('dockedSidebar');
                        }
                    }

                    // Set theme if specified
                    const theme = '%s';
                    const currentTheme = localStorage.getItem('selectedTheme') || '';

                    if (theme !== currentTheme) {
                        if (theme !== "") {
                            localStorage.setItem('selectedTheme', theme);
                        } else {
                            localStorage.removeItem('selectedTheme');
                        }
                    }
//                    console.log("Setting sidebar: " + currentSidebar + " -> " + sidebar + " [Result=" + localStorage.getItem('dockedSidebar') +
//		                "]; theme: " + currentTheme + " -> " + theme + " [Result=" + localStorage.getItem('selectedTheme') + "]"); // DEBUG

//                  localStorage.setItem('DebugLog', "Setting sidebar: " + currentSidebar + " -> " + sidebar + " [Result=" + localStorage.getItem('dockedSidebar') +
//		                                     "]; theme: " + currentTheme + " -> " + theme + "[Result=" + localStorage.getItem('selectedTheme') + "]"); // DEBUG
                } catch (err) {
                    console.error(err);
                    console.log("FAILED to set: Sidebar: " + sidebar + "  Theme: " + theme + " [" + err + "]"); // DEBUG
                    localStorage.setItem('DebugLog', "FAILED to set: Sidebar: " + sidebar + "  Theme: " + theme); // DEBUG
                }
            ]], single_quote_escape(sidebar), single_quote_escape(theme))

            v:eval_js(js_settings, { source = "ha_settings.js", no_return = true })
            msg.info("Applying HA settings on dashboard %s: theme=%s sidebar=%s", v.uri, theme, sidebar) -- DEBUG

            ha_settings_applied[v] = true   -- Mark in Lua session as settings applied
        end

        -- Re-run once the page has fully finished loading, as a fallback in
        -- case any shadow roots were attached before the "committed"-stage
        -- injection above installed its attachShadow patch
        v:eval_js(js_disable_animations, { source = "disable_animations.js", no_return = true })

        -- Suppress known harmless unhandled promise rejections in kiosk environment
        --   - Service worker / script load failures during reloads
        --   - View transition errors when monitor/document is hidden (common when screen off)
        -- Prevents page aborts/504s while keeping real errors visible
        local js_suppress_errors = [[
            window.addEventListener('unhandledrejection', function(e) {
                const reason = e.reason;
                let suppress = false;

                if (reason) {
                    const msg = typeof reason.message === 'string' ? reason.message : '';
                    const name = (reason.name || '').toLowerCase();

                    if (msg.includes('sw-modern.js') ||
                        msg.includes('load failed') ||
                        msg.includes('service worker') ||
                        name === 'invalidstateerror' &&
                            (msg.includes('document visibility state is hidden') ||
                             msg.includes('view transition')) ||
                        reason === '[object Object]' ||
                        msg === '' ||                    // Empty message common in HA reconnect bugs
                        typeof reason === 'object') {    // Catch generic objects
                        suppress = true;
                    }
                }

                if (suppress) {
                    console.warn('Suppressed known kiosk-safe unhandled rejection:', reason);
                    e.preventDefault(); // Prevent abort, potential load failure or error cascade
                }
            });
        ]]

        -- Inject suppress_errors script into the webview (once per load-finished)
        v:eval_js(js_suppress_errors, { source = "suppress_kiosk_errors.js", no_return = true })

        -- Add HA websocket recovery monitor and force reload if dead (common after reconnect failures)
        local js_ws_recovery = [[
            (function() {
                if (window.ha_ws_recovery_interval) return;  // Only once
                window.ha_ws_recovery_interval = setInterval(function() {
                    if (window.APP && window.APP.connection && !window.APP.connection.connected) {
                        console.warn('HA websocket dead >10s - forcing reload for recovery');
                        location.reload();
                    }
                }, 10000);  // Check every 10 seconds
            })();
        ]]

        -- Inject websocket recovery monitor script into the webview (once per load-finished)
        v:eval_js(js_ws_recovery, { source = "ws_recovery.js", no_return = true })

    end)

    -- If browser_refresh set, then refresh browser every browser_refresh seconds after page finished/loaded/reloaded
    if browser_refresh > 0 then

       -- Check page visibility and set per-view flag so can skip refreshing non-visible pages
        --[=[  COMMENT-OUT FOR NOW since kiosks typically have only a single visible page
        local page_visible = true  -- Per-view visibility (optimistic default to 'true')

        -- Inject JS on every load-finished
        view:add_signal("load-status", function(v, status)
            if status ~= "finished" then return end

            -- Evaluate document.visibilityState and update page_visible
            v:eval_js([[
                (function() {
                    return document.visibilityState;
                })();
            ]], {
                callback = function(state)
                    page_visible = (state == "visible")
                    msg.info("DEBUG: page visibility set to '%s': %s", state, v.uri) -- DEBUG
                end,
                error_callback = function(err)
                    msg.warn("ERROR: Couldn't determine page visibility: %s (%s)", v.uri, err)
                end,
            })
        end)
	]=]

	-- Refresh browser logic
        local refresh_timer = nil  -- Per-view refresh timer
        local hard_reload_count = 0
        local function reset_refresh_timer()
            if not view.uri or view.uri == "about:blank" then return end -- Invalid or blank URL

            if refresh_timer then  -- Refresh existing timer
                msg.info("Restarting refresh timer (%ds): %s", browser_refresh, view.uri)
		refresh_timer:stop()   -- Stop current countdown
		refresh_timer:start()  -- Restart from full interval
            else  -- Initialize new timer
                msg.info("Initializing refresh timer (%ds): %s", browser_refresh, view.uri)
                refresh_timer = timer { interval = browser_refresh * 1000 }
                refresh_timer:add_signal("timeout", function(t)
                    if not view.is_alive then
                        msg.info("DEBUG: Skipping reload - webview not alive [shouldn't happen]")
                        return
                    end

		    if not view.uri or view.uri == "about:blank" then return end

		    --[=[ COMMENT-OUT if not testing visibility
		    if not page_visible then
		        msg.info("Skipping reload - page not visible")
			return
                    end
		    ]=]

  		    hard_reload_count = hard_reload_count + 1
  		    local bypass_cache = (hard_reload_count % HARD_RELOAD_FREQ == 0)  -- Hard reload  every 10th
                    msg.info("RELOADING%s: %s", bypass_cache and " [HARD]" or "", view.uri)
       		    view:reload(bypass_cache)
                end)
                refresh_timer:start()
            end
        end

        -- Initial set refresh timer (in case already loaded)
        reset_refresh_timer()

        -- Start/restart on finished loads when URI is valid
        view:add_signal("load-status", function(v, status)
            if status ~= "finished" then return end
	    reset_refresh_timer()
        end)

        -- Also on manual reloads
        view:add_signal("reload", function()
            reset_refresh_timer()
        end)

        -- *** CLEANUP: Stop and delete refresh timer when this webview is destroyed ***
        view:add_signal("destroy", function()
            if refresh_timer then
                msg.info("DEBUG: Webview destroyed - stopping and discarding refresh timer")
                refresh_timer:stop()
                refresh_timer = nil  -- Allow garbage collection
            end
        end)

    end
end)
-- -----------------------------------------------------------------------
-- Screensaver: after $SCREENSAVER_TIMEOUT idle seconds, show a fullscreen
-- slideshow of images pulled live from Home Assistant's local Media source
-- (folder name set by $SCREENSAVER_MEDIA_FOLDER). Images can be uploaded
-- remotely to that folder via the HA mobile app or web UI's Media page -
-- no separate file share or add-on required. Any touch/mouse/key activity
-- dismisses the screensaver and resets the idle timer.
if screensaver_enabled then
    local js_screensaver = string.format([[
        (function() {
            if (window.haoskiosk_screensaver_installed) return;
            window.haoskiosk_screensaver_installed = true;

            var TIMEOUT_MS = %d;
            var INTERVAL_MS = %d;
            var FOLDER = '%s';
            var MEDIA_STYLE = 'max-width:100%%;max-height:100%%;object-fit:contain;';
            var overlay = null, slideTimer = null, idleTimer = null, images = [], idx = 0;
            var bitmapDownscaleSupported = (typeof createImageBitmap === 'function');

            function getHass() {
                var el = document.querySelector('home-assistant');
                return el && el.hass;
            }

            // Decode+downscale off-DOM via createImageBitmap so large photos (e.g. multi-MB
            // phone camera originals) never get fully decoded/retained at full resolution -
            // avoids OOM/crashes on memory-constrained devices like the Pi. Falls back to a
            // plain <img> (browser-native decode) if the WebKit build lacks resize support.
            function renderViaBitmap(url) {
                var maxDim = Math.round(Math.min(Math.max(window.innerWidth, window.innerHeight) * (window.devicePixelRatio || 1), 1920));
                return fetch(url)
                    .then(function(r) { return r.blob(); })
                    .then(function(blob) { return createImageBitmap(blob, { resizeWidth: maxDim, resizeQuality: 'medium' }); })
                    .then(function(bitmap) {
                        var canvas = document.createElement('canvas');
                        canvas.width = bitmap.width;
                        canvas.height = bitmap.height;
                        canvas.style.cssText = MEDIA_STYLE;
                        canvas.getContext('2d').drawImage(bitmap, 0, 0);
                        bitmap.close();
                        overlay.innerHTML = '';
                        overlay.appendChild(canvas);
                    });
            }

            function renderViaImg(url) {
                return new Promise(function(resolve, reject) {
                    var image = new Image();
                    image.style.cssText = MEDIA_STYLE;
                    image.onload = function() {
                        overlay.innerHTML = '';
                        overlay.appendChild(image);
                        resolve();
                    };
                    image.onerror = reject;
                    image.src = url;
                });
            }

            function showImage(url) {
                if (!overlay) return;
                var render = bitmapDownscaleSupported ? renderViaBitmap(url) : renderViaImg(url);
                render.catch(function(err) {
                    if (bitmapDownscaleSupported) {
                        console.warn('Screensaver: bitmap downscale failed, falling back to <img>', err);
                        bitmapDownscaleSupported = false;
                        return renderViaImg(url).catch(function(err2) {
                            console.warn('Screensaver: failed to render image: ' + url, err2);
                        });
                    }
                    console.warn('Screensaver: failed to render image: ' + url, err);
                });
            }

            function showNextImage() {
                if (!images.length) return;
                idx = (idx + 1) %% images.length;
                showImage(images[idx]);
            }

            function stopSlideshow() {
                if (slideTimer) { clearInterval(slideTimer); slideTimer = null; }
                if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
                overlay = null;
                images = [];
            }

            function startSlideshow() {
                var hass = getHass();
                if (!hass) return;

                overlay = document.createElement('div');
                overlay.id = 'haoskiosk-screensaver';
                overlay.style.cssText = 'position:fixed;inset:0;z-index:2147483647;background:#000;display:flex;align-items:center;justify-content:center;';
                document.body.appendChild(overlay);

                var mediaContentId = 'media-source://media_source/local' + (FOLDER ? '/' + FOLDER : '');
                hass.callWS({type: 'media_source/browse_media', media_content_id: mediaContentId})
                    .then(function(result) {
                        var children = (result.children || []).filter(function(c) {
                            return c.media_class === 'image' || (c.media_content_type || '').indexOf('image') === 0;
                        });
                        return Promise.all(children.map(function(c) {
                            return hass.callWS({type: 'media_source/resolve_media', media_content_id: c.media_content_id})
                                .then(function(r) { return new URL(r.url, location.origin).href; })
                                .catch(function() { return null; });
                        }));
                    })
                    .then(function(urls) {
                        images = urls.filter(Boolean);
                        if (!images.length) {
                            console.warn('Screensaver: no images found in local media: ' + mediaContentId);
                            stopSlideshow();
                            return;
                        }
                        idx = Math.floor(Math.random() * images.length);
                        showImage(images[idx]);
                        slideTimer = setInterval(showNextImage, INTERVAL_MS);
                    })
                    .catch(function(err) {
                        console.warn('Screensaver: failed to browse local media: ' + mediaContentId, err);
                        stopSlideshow();
                    });
            }

            function resetIdleTimer() {
                if (overlay) stopSlideshow();
                if (idleTimer) clearTimeout(idleTimer);
                idleTimer = setTimeout(startSlideshow, TIMEOUT_MS);
            }

            ['mousedown', 'mousemove', 'touchstart', 'keydown', 'wheel'].forEach(function(evt) {
                window.addEventListener(evt, resetIdleTimer, {capture: true, passive: true});
            });

            resetIdleTimer();
        })();
    ]], screensaver_timeout * 1000, screensaver_interval * 1000, single_quote_escape(screensaver_media_folder))

    webview.add_signal("init", function(view)
        view:add_signal("load-status", function(v, status)
            if status ~= "finished" then return end
            v:eval_js(js_screensaver, { source = "screensaver.js", no_return = true })
        end)
    end)
end
-- -----------------------------------------------------------------------
-- Tab and Window functions


-- Close window unless last window (to avoid quitting luakit)
local function close_win_not_last(w)
    if #luakit.windows > 1 then
        w:close_win()
    else
        msg.warn("WARNING: This is the last window — not closing.")
    end
end

-- -----------------------------------------------------------------------
-- Redefine <Esc> to 'new_escape_key' (e.g., Ctl+Alt+Esc>) to exit current mode and enter normal mode
--
modes.remove_binds({"passthrough"}, {"<Escape>"})

modes.add_binds("all", {   -- Add to all modes (note  modes other than 'passhtrough' still accept Escape too)
    {new_escape_key, "Switch to normal mode", function(w)
        w:set_prompt()
        w:set_mode() -- Use this if not redefining 'default_mode' since defaults to "normal"
     end },
})

-- Clear the command line when entering passthrough instead of typing '-- PASS THROUGH --'
modes.get_modes()["passthrough"].enter = function(w)
    w:set_prompt()            -- Clear the command line prompt
    w:set_input()             -- Activate the input field (e.g., URL bar or form)
    w.view.can_focus = true   -- Ensure the webview can receive focus
    w.view:focus()            -- Focus the webview for keyboard input
end

-- -----------------------------------------------------------------------
modes.add_binds("all", {
    -- Browser history and reload
    { "<Control-r>",                    "Reload page",                          function(w) w:reload() end },
    { "<Control-Left>",                 "Go back in the browser history",       function(w, m) w:back(m.count) end },
    { "<Control-Right>",                "Go forward in the browser history",    function(w, m) w:forward(m.count) end },

    -- New/Close tab and window
    { "<Control-Mod1-t>",               "Open new tab",                         function(w) w:new_tab() end },
    { "<Control-Mod1-Shift-t>",         "Close current tab",                    function(w) w:close_tab() end },
    { "<Control-Mod1-w>",               "Open new window",                      function() window.new() end },
--    { "<Control-Mod1-Shift-w>",         "Close current window",                 function(w) w:close_win() end },
    { "<Control-Mod1-Shift-w>",         "Close current window",                 function(w) close_win_not_last(w) end },

    -- Tab navigation
    { "<Control-Mod1-Left>",            "Go to previous tab",                  function(w) w:prev_tab() end },
    { "<Control-Mod1-Right>",           "Go to next tab",                      function(w) w:next_tab() end },

    -- Window navigation (Use Window manager bindings)
    -- Ctrl+Alt+Shift+Left (or Shift+Alt+Tab) for "Go to previous window"
    -- Ctrl+Alt+Sift+Right (or Alt+Tab) for "Go to next window"
})

-- -----------------------------------------------------------------------
