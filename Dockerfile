# igscrape in a container: the CLI with a Camoufox browser on a virtual X display
# (Xvfb), viewable over noVNC for logging accounts in by hand. See DOCKER.md.
#
# Headful-in-Xvfb is the recommended mode: it keeps Camoufox's stealth fingerprint
# intact on a server with no screen. --headless also works: on Linux igscrape turns
# it into camoufox's headless="virtual", the same headful browser inside an Xvfb
# display camoufox starts itself, so xvfb is needed either way.
FROM python:3.12-slim-bookworm

# Match the host user so bind-mounted db/ and data/ stay owned by it.
ARG UID=1000
ARG GID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DISPLAY=:99 \
    SCREEN_RES=1280x800x24

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2 \
        libx11-xcb1 libxcb-shm0 libxcomposite1 libxcursor1 libxdamage1 \
        libxfixes3 libxi6 libxrandr2 libxtst6 libnss3 libpango-1.0-0 \
        fonts-liberation fonts-noto-core \
        xvfb x11vnc fluxbox x11-utils novnc websockify \
        ffmpeg \
        tini ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -o -g ${GID} scraper \
 && useradd -o -m -u ${UID} -g ${GID} -s /bin/bash scraper

# Non-root Xvfb needs this as a sticky world-writable dir, or it prints
# `_XSERVTransmkdir: ERROR: euid != 0`.
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

WORKDIR /app

# Dependencies from uv.lock first, so a code change doesn't reinstall them.
COPY --from=ghcr.io/astral-sh/uv:0.9.15 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --no-hashes -o /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt

# Editable, so the package resolves its home dir to /app: db/ and data/ below are
# where the CLI looks for accounts.db and writes results by default.
COPY README.md LICENSE ./
COPY igscrape ./igscrape
RUN pip install --no-cache-dir --no-deps -e .

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
 && mkdir -p /app/db /app/data \
 && chown -R scraper:scraper /app

USER scraper

# camoufox fetch fails intermittently mid-download and can leave a partial browser
# behind, which would only surface later as a scrape failure; retry, then check
# the browser is really there before the layer commits.
RUN ( n=0; until python -m camoufox fetch; do n=$((n+1)); \
        [ "$n" -ge 5 ] && echo "FATAL: camoufox fetch failed after $n attempts" && exit 1; \
        echo "camoufox fetch retry $n ..."; sleep 10; done ) \
 && [ -n "$(ls -A /home/scraper/.cache/camoufox 2>/dev/null)" ]

EXPOSE 6080
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
