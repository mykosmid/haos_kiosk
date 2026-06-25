ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM $BUILD_FROM

ARG BUILD_VERSION
ENV ADDON_VERSION=${BUILD_VERSION}

RUN ALPINE_VER=$(cat /etc/alpine-release 2>/dev/null | cut -d. -f1-2) && \
    echo "https://dl-cdn.alpinelinux.org/alpine/$ALPINE_VER/community" >> /etc/apk/repositories && \
    apk add --no-cache \
    xorg-server \
    xf86-video-modesetting \
    xf86-input-libinput \
    libinput \
    udev \
    libinput-udev \
    xinput \
    xrandr \
    xset \
    libxkbcommon \
    setxkbmap \
    mesa-dri-gallium \
    mesa-egl \
    mesa-gles \
    libdrm \
    bash \
    util-linux \
    luakit \
    && rm -rf /var/cache/apk/*

ENV DISPLAY=:0
ENV WEBKIT_DISABLE_COMPOSITING_MODE=1
ENV WEBKIT_FORCE_COMPLEX_TEXT=1
ENV NO_AT_BRIDGE=1

COPY xorg.conf.default /etc/X11/
COPY userconf.lua /root/.config/luakit/
COPY translations/*.yaml /translations/

COPY run.sh /
RUN chmod a+x /run.sh

CMD ["/run.sh"]
