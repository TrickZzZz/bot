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
    # /var/lib/cloudflare-warp should be a persistent Railway Volume. Without
    # one, every container restart registers as a brand-new device — quietly
    # burning through the 5-device WARP+ license limit.
    #
    # Rather than pre-checking whether a registration already exists (which
    # proved unreliable — warp-cli's own state isn't always ready to answer
    # that immediately after startup), just attempt registration directly.
    # If one already exists on the persisted volume, warp-cli tells us so
    # with a specific, recognizable message — treat that as success (reuse
    # the existing device) rather than as a failure.
    reg_output=$(warp-cli --accept-tos registration new 2>&1)
    reg_status=$?

    if [ $reg_status -eq 0 ]; then
        echo "[entrypoint] registered as a new device (uses one WARP+ device slot, one time only if the volume persists)"
        if [ -n "$ROBLOX_WARP_LICENSE_KEY" ]; then
            echo "[entrypoint] applying WARP+ license..."
            warp-cli --accept-tos registration license "$ROBLOX_WARP_LICENSE_KEY" || true
        else
            echo "[entrypoint] no ROBLOX_WARP_LICENSE_KEY set — running on free tier"
        fi
    elif echo "$reg_output" | grep -qi "Old registration is still around"; then
        echo "[entrypoint] existing WARP registration found on persistent volume — reusing it, no device slot used"
    else
        echo "[entrypoint] registration failed: $reg_output"
        return 1
    fi

    warp-cli --accept-tos mode proxy || return 1
    warp-cli --accept-tos proxy port 40000 || return 1

    # Proxy mode only supports MASQUE — a previous experiment set this to
    # WireGuard, and that setting persisted on the volume just like the
    # device registration does. Force it back explicitly every boot rather
    # than assuming it defaults to MASQUE.
    warp-cli --accept-tos tunnel protocol set MASQUE || true

    warp-cli --accept-tos connect || return 1
    return 0
}

if setup_warp; then
    echo "[entrypoint] WARP configured — testing whether the proxy actually works..."
    proxy_working=0
    for i in $(seq 1 60); do
        # Test the ACTUAL thing we need — can a real request reach Cloudflare
        # through 127.0.0.1:40000 — rather than trusting warp-cli's own
        # internal "Connected" status, which depends on a separate internal
        # self-check that can fail/timeout for unrelated reasons even when
        # the proxy itself is perfectly usable.
        trace=$(curl --socks5-hostname 127.0.0.1:40000 -s --max-time 5 https://cloudflare.com/cdn-cgi/trace 2>/dev/null)
        if echo "$trace" | grep -qE "warp=(plus|on)"; then
            echo "[entrypoint] WARP proxy confirmed working on 127.0.0.1:40000 ($(echo "$trace" | grep '^warp='))"
            proxy_working=1
            break
        fi
        sleep 1
    done
    if [ "$proxy_working" -eq 0 ]; then
        echo "[entrypoint] proxy did not confirm working within 60s — continuing without it"
        echo "[entrypoint] --- warp-cli status (full output) ---"
        warp-cli --accept-tos status 2>&1 || echo "[entrypoint] (status command itself failed)"
        echo "[entrypoint] --- warp-svc.log (last 40 lines) ---"
        tail -n 40 /var/log/warp-svc.log 2>&1 || echo "[entrypoint] (no warp-svc.log found)"
        echo "[entrypoint] --- end diagnostics ---"
    fi
else
    echo "[entrypoint] WARP setup failed — continuing without it. Roblox calls fall back to the residential proxy only."
    echo "[entrypoint] --- warp-svc.log (last 40 lines) ---"
    tail -n 40 /var/log/warp-svc.log 2>&1 || echo "[entrypoint] (no warp-svc.log found)"
    echo "[entrypoint] --- end diagnostics ---"
fi

# Always start the real application, regardless of WARP's outcome above.
echo "[entrypoint] starting uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
