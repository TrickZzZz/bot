#!/bin/bash
# Deliberately NOT using `set -e` here. A WARP setup failure must never
# prevent the actual web server from starting — every WARP-related step
# below is best-effort, logged, and allowed to fail without stopping boot.

echo "[entrypoint] starting WARP daemon (best-effort)..."
warp-svc >/var/log/warp-svc.log 2>&1 &

# Give warp-svc a moment to create its control socket before warp-cli tries
# to talk to it.
sleep 2

setup_warp() {
    warp-cli --accept-tos registration new || return 1

    if [ -n "$ROBLOX_WARP_LICENSE_KEY" ]; then
        echo "[entrypoint] applying WARP+ license..."
        warp-cli --accept-tos registration license "$ROBLOX_WARP_LICENSE_KEY" || true
    else
        echo "[entrypoint] no ROBLOX_WARP_LICENSE_KEY set — running on free tier"
    fi

    warp-cli --accept-tos mode proxy || return 1
    warp-cli --accept-tos proxy port 40000 || return 1
    warp-cli --accept-tos connect || return 1
    return 0
}

if setup_warp; then
    echo "[entrypoint] WARP configured — waiting for connection..."
    connected=0
    for i in $(seq 1 20); do
        if warp-cli --accept-tos status 2>/dev/null | grep -qi "Connected"; then
            echo "[entrypoint] WARP connected on 127.0.0.1:40000"
            connected=1
            break
        fi
        sleep 1
    done
    if [ "$connected" -eq 0 ]; then
        echo "[entrypoint] WARP did not report Connected within 20s — continuing without it"
    fi
else
    echo "[entrypoint] WARP setup failed — continuing without it. Roblox calls fall back to the residential proxy only."
fi

# Always start the real application, regardless of WARP's outcome above.
echo "[entrypoint] starting uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
