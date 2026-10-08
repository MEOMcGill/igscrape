#!/usr/bin/env bash
# Starts an X display for the container, then runs the command.
#
# Headful scrapes render to it. With VNC=1 the display is also served over noVNC,
# which is how an account is logged in by hand (or a checkpoint solved):
# http://localhost:${NOVNC_PORT}/vnc.html, through an ssh port forward on a server.
set -euo pipefail

DISPLAY_NUM="${DISPLAY:-:99}"
RES="${SCREEN_RES:-1280x800x24}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"

# A stale lock left by a previous container on the same writable layer would
# stop Xvfb claiming the display.
rm -f "/tmp/.X${DISPLAY_NUM#:}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM#:}" 2>/dev/null || true

Xvfb "$DISPLAY_NUM" -screen 0 "$RES" -ac +extension RANDR -nolisten tcp >/dev/null 2>&1 &
for _ in $(seq 1 30); do
    if xdpyinfo -display "$DISPLAY_NUM" >/dev/null 2>&1; then break; fi
    sleep 0.1
done

if [ "${VNC:-0}" = "1" ]; then
    fluxbox -display "$DISPLAY_NUM" >/dev/null 2>&1 &
    x11vnc -display "$DISPLAY_NUM" -nopw -forever -shared \
           -rfbport "$VNC_PORT" -bg -quiet -o /tmp/x11vnc.log
    websockify --web=/usr/share/novnc "$NOVNC_PORT" "localhost:$VNC_PORT" \
           >/tmp/websockify.log 2>&1 &
    echo "noVNC: http://localhost:${NOVNC_PORT}/vnc.html (display ${DISPLAY_NUM}, ${RES})"
fi

exec "$@"
