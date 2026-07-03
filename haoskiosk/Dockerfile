ARG BUILD_FROM=ghcr.io/home-assistant/base:latest

# ---- builder: compile the two pip-only Python deps (evdev, websockets) ----
# These aren't guaranteed to exist as prebuilt Alpine packages on every base
# image/architecture combo this add-on ships for, so they're built here from
# PyPI with a full toolchain and copied into the runtime image as plain
# files - the compilers themselves never end up in the final image.
FROM $BUILD_FROM AS builder

RUN apk add --no-cache python3 py3-pip build-base python3-dev linux-headers

RUN pip install --no-cache-dir --target=/pylibs evdev websockets || \
    pip install --no-cache-dir --break-system-packages --target=/pylibs evdev websockets

# ---- runtime image ----
FROM $BUILD_FROM

ARG BUILD_VERSION
ENV ADDON_VERSION=${BUILD_VERSION}

RUN ALPINE_VER=$(cat /etc/alpine-release 2>/dev/null | cut -d. -f1-2) && \
    echo "https://dl-cdn.alpinelinux.org/alpine/$ALPINE_VER/community" >> /etc/apk/repositories && \
    apk add --no-cache \
    bash \
    python3 \
    py3-pillow \
    font-dejavu \
    && rm -rf /var/cache/apk/*

COPY --from=builder /pylibs /usr/lib/python3-extra
ENV PYTHONPATH=/usr/lib/python3-extra

COPY app/kiosk.py app/ha_client.py /
COPY translations/*.yaml /translations/

COPY run.sh /
RUN chmod a+x /run.sh

CMD ["/run.sh"]
