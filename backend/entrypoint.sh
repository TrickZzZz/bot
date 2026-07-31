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
    elif echo "$reg_output" | grep -qi "Old registration is still around"; then
        echo "[entrypoint] existing WARP registration found on persistent volume — reusing it, no device slot used"
    else
        echo "[entrypoint] registration failed: $reg_output"
        return 1
    fi

    # Apply the license EVERY boot, regardless of whether the registration
    # above was fresh or reused. Without this, changing ROBLOX_WARP_LICENSE_KEY
    # to a new key would silently do nothing on an already-registered device —
    # the old (fresh-device-only) logic only ever applied a license at the
    # moment of first registration. Re-applying the same key is a harmless
    # no-op; applying a genuinely new key updates it correctly.
    if [ -n "$ROBLOX_WARP_LICENSE_KEY" ]; then
        echo "[entrypoint] applying WARP+ license..."
        license_output=$(warp-cli --accept-tos registration license "$ROBLOX_WARP_LICENSE_KEY" 2>&1)
        echo "[entrypoint] license result: $license_output"
    else
        echo "[entrypoint] no ROBLOX_WARP_LICENSE_KEY set — running on free tier"
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

# Test the ACTUAL thing we need — can a real request reach Cloudflare through
# 127.0.0.1:40000 — rather than trusting warp-cli's own internal "Connected"
# status, which depends on a separate internal self-check that can fail for
# unrelated reasons even when the proxy is perfectly usable.
test_warp_proxy() {
    local trace
    trace=$(curl --socks5-hostname 127.0.0.1:40000 -s --max-time 5 https://cloudflare.com/cdn-cgi/trace 2>/dev/null)
    echo "$trace" | grep -qE "warp=(plus|on)"
}

warp_ready=0

if setup_warp; then
    # We've directly observed the SAME setup succeed on one boot and fail
    # badly on the next — this looks like real, moment-to-moment network
    # variability rather than a config problem. So don't just wait once —
    # actively retry the connection a few times before giving up, since a
    # bad attempt now doesn't mean the next one will be bad too.
    for attempt in 1 2 3; do
        echo "[entrypoint] testing WARP proxy — attempt $attempt/3..."
        found=0
        for i in $(seq 1 20); do
            if test_warp_proxy; then
                found=1
                break
            fi
            sleep 1
        done
        if [ "$found" -eq 1 ]; then
            echo "[entrypoint] WARP proxy confirmed working on attempt $attempt"
            warp_ready=1
            break
        fi
        if [ "$attempt" -lt 3 ]; then
            echo "[entrypoint] attempt $attempt did not confirm — disconnecting and retrying..."
            warp-cli --accept-tos disconnect >/dev/null 2>&1
            sleep 2
            warp-cli --accept-tos connect >/dev/null 2>&1
        fi
    done
fi

if [ "$warp_ready" -eq 1 ]; then
    echo "[entrypoint] WARP is ready"
else
    echo "[entrypoint] WARP did not confirm working after 3 attempts — continuing without it for now. Roblox calls fall back to the residential proxy only. The background watchdog below will keep trying."
    echo "[entrypoint] --- warp-cli status (full output) ---"
    warp-cli --accept-tos status 2>&1 || echo "[entrypoint] (status command itself failed)"
    echo "[entrypoint] --- warp-svc.log (last 30 lines) ---"
    tail -n 30 /var/log/warp-svc.log 2>&1 || echo "[entrypoint] (no warp-svc.log found)"
    echo "[entrypoint] --- end diagnostics ---"
fi

# Background watchdog — keeps trying to (re)establish a working WARP
# connection for the ENTIRE lifetime of the container, independent of what
# happened at boot. A boot that failed can self-heal a few minutes later;
# a boot that succeeded can also degrade and recover. Runs forever in the
# background alongside uvicorn; every check and reconnect attempt is logged
# so it's visible in Railway's log tail.
(
    while true; do
        sleep 120
        if test_warp_proxy; then
            : # still (or again) working — nothing to do
        else
            echo "[warp-watchdog] proxy not responding — attempting reconnect..."
            warp-cli --accept-tos connect >/dev/null 2>&1
            sleep 15
            if test_warp_proxy; then
                echo "[warp-watchdog] reconnect succeeded — WARP is usable again"
            else
                echo "[warp-watchdog] reconnect did not restore a working proxy — will check again in 2 minutes"
            fi
        fi
    done
) &

# Always start the real application, regardless of WARP's outcome above.
echo "[entrypoint] starting uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
