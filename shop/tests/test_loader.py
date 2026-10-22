"""Tests for the two-stage hosted loader (app/loader.py and the /l/ and /p/ routes in app/main.py).

The last class runs stage 1 for real in Lua 5.3 (texlua) with a stubbed `game`, when texlua is installed.

Run from the shop folder:  python3 -m unittest discover -s tests -v
"""
import hashlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from test_scripts import SCRIPT, ScriptCase, main  # noqa: E402

loader = main.loader
SECRET = "a" * 32
NONCE = "b" * 32


class CryptoTests(unittest.TestCase):
    def test_round_trip_for_empty_short_block_edge_and_large_scripts(self):
        for size in (0, 1, 23, 24, 25, 32, 33, 1000, 200_000):
            raw = bytes((i * 7) % 256 for i in range(size))
            payload = loader.encrypt(SECRET, NONCE, raw)
            self.assertEqual(loader.decrypt(SECRET, NONCE, payload), raw, size)

    def test_payload_is_plain_base64_text_that_hides_the_script(self):
        payload = loader.encrypt(SECRET, NONCE, SCRIPT.encode())
        self.assertNotIn(b"hello", payload)
        self.assertRegex(payload.decode(), r"^[A-Za-z0-9+/=]+$")

    def test_a_different_nonce_or_secret_gives_a_different_payload_and_does_not_decrypt(self):
        raw = SCRIPT.encode()
        first = loader.encrypt(SECRET, NONCE, raw)
        self.assertNotEqual(first, loader.encrypt(SECRET, "c" * 32, raw))
        self.assertNotEqual(first, loader.encrypt("d" * 32, NONCE, raw))
        self.assertIsNone(loader.decrypt(SECRET, "c" * 32, first))
        self.assertIsNone(loader.decrypt("d" * 32, NONCE, first))

    def test_a_changed_or_cut_payload_is_refused(self):
        good = loader.encrypt(SECRET, NONCE, b"x" * 100)
        changed = bytearray(good)
        changed[10] = ord("A") if changed[10] != ord("A") else ord("B")
        self.assertNotEqual(loader.decrypt(SECRET, NONCE, bytes(changed)), b"x" * 100)
        self.assertIsNone(loader.decrypt(SECRET, NONCE, good[:-8]))
        self.assertIsNone(loader.decrypt(SECRET, NONCE, b"not base64 !!"))

    def test_signature_matches_only_its_own_token_and_nonce(self):
        sig = loader.sign(SECRET, "tok", NONCE)
        self.assertTrue(loader.signature_ok(SECRET, "tok", NONCE, sig))
        self.assertFalse(loader.signature_ok(SECRET, "other", NONCE, sig))
        self.assertFalse(loader.signature_ok(SECRET, "tok", "c" * 32, sig))
        self.assertFalse(loader.signature_ok("d" * 32, "tok", NONCE, sig))
        self.assertRegex(sig, loader.SIG_RE.pattern)

    def test_keys_come_from_the_env_secret_first_then_the_admin_password_and_short_ones_are_ignored(self):
        long_a, long_b = "x" * 30, "y" * 30
        self.assertIsNone(loader.master_key("", ""))
        self.assertIsNone(loader.master_key("short", "short"))
        self.assertEqual(loader.master_key(long_a, long_b), loader.master_key(long_a, ""))
        self.assertNotEqual(loader.master_key(long_a, ""), loader.master_key("", long_a))
        self.assertIsNotNone(loader.master_key("short", long_b))
        master = loader.master_key(long_a, "")
        self.assertNotEqual(loader.link_secret(master, "one"), loader.link_secret(master, "two"))


class NonceTests(unittest.TestCase):
    def test_a_nonce_works_once_for_its_own_token(self):
        store = loader.NonceStore()
        nonce = store.issue("tok")
        self.assertRegex(nonce, loader.NONCE_RE.pattern)
        self.assertTrue(store.take("tok", nonce))
        self.assertFalse(store.take("tok", nonce))

    def test_a_nonce_for_another_token_is_refused_and_spent(self):
        store = loader.NonceStore()
        nonce = store.issue("tok")
        self.assertFalse(store.take("other", nonce))
        self.assertFalse(store.take("tok", nonce))

    def test_a_nonce_expires(self):
        now = [1000.0]
        store = loader.NonceStore(ttl=120, clock=lambda: now[0])
        nonce = store.issue("tok")
        now[0] += 121
        self.assertFalse(store.take("tok", nonce))
        fresh = store.issue("tok")
        now[0] += 119
        self.assertTrue(store.take("tok", fresh))

    def test_unknown_nonce_is_refused(self):
        self.assertFalse(loader.NonceStore().take("tok", NONCE))

    def test_the_store_stays_under_its_cap(self):
        store = loader.NonceStore(cap=100)
        for _ in range(450):
            store.issue("tok")
        self.assertLessEqual(len(store), 100)
        newest = store.issue("tok")
        self.assertTrue(store.take("tok", newest))


class RouteTests(ScriptCase):
    def test_stage_one_carries_no_script_text_and_is_plain_text(self):
        token, _ = self.add("v4", "print('SECRET-MARKER-123')\n")
        resp = self.stage_one(token)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["content-type"].startswith("text/plain"))
        self.assertEqual(resp.headers["cache-control"], "no-store")
        self.assertNotIn(b"SECRET-MARKER-123", resp.body)
        base, found_token, nonce, _secret = self.parse_stage(resp)
        self.assertEqual((base, found_token), ("https://shop.example", token))
        self.assertRegex(nonce, loader.NONCE_RE.pattern)

    def test_every_stage_one_gets_its_own_nonce(self):
        token, _ = self.add()
        nonces = {self.parse_stage(self.stage_one(token))[2] for _ in range(5)}
        self.assertEqual(len(nonces), 5)

    def test_the_payload_is_encrypted_on_the_wire(self):
        token, _ = self.add("v4", "print('SECRET-MARKER-123')\n")
        _b, _t, nonce, secret = self.parse_stage(self.stage_one(token))
        resp = self.payload(token, nonce, loader.sign(secret, token, nonce))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["cache-control"], "no-store")
        self.assertNotIn(b"SECRET-MARKER-123", resp.body)
        self.assertEqual(loader.decrypt(secret, nonce, resp.body), b"print('SECRET-MARKER-123')\n")

    def test_a_nonce_cannot_be_used_twice(self):
        token, _ = self.add()
        _b, _t, nonce, secret = self.parse_stage(self.stage_one(token))
        sig = loader.sign(secret, token, nonce)
        self.assertEqual(self.payload(token, nonce, sig).status_code, 200)
        self.assertEqual(self.payload(token, nonce, sig).status_code, 404)

    def test_a_bad_signature_is_a_404_and_does_not_burn_the_real_request(self):
        token, _ = self.add()
        _b, _t, nonce, secret = self.parse_stage(self.stage_one(token))
        self.assertEqual(self.payload(token, nonce, "0" * 64).status_code, 404)
        self.assertEqual(self.payload(token, nonce, loader.sign(secret, token, nonce)).status_code, 200)

    def test_a_signature_for_another_link_does_not_work(self):
        first, _ = self.add("one")
        second, _ = self.add("two")
        _b, _t, nonce, secret = self.parse_stage(self.stage_one(first))
        self.assertEqual(self.payload(second, nonce, loader.sign(secret, second, nonce)).status_code, 404)

    def test_an_expired_nonce_is_refused(self):
        token, _ = self.add()
        _b, _t, nonce, secret = self.parse_stage(self.stage_one(token))
        real = main.NONCES._clock
        self.patch(main.NONCES, "_clock", lambda: real() + loader.NONCE_TTL + 5)
        self.assertEqual(self.payload(token, nonce, loader.sign(secret, token, nonce)).status_code, 404)

    def test_every_refusal_is_the_same_plain_404(self):
        token, _ = self.add()
        _b, _t, nonce, secret = self.parse_stage(self.stage_one(token))
        good = loader.sign(secret, token, nonce)
        answers = [
            self.call(main.script_payload, f"/p/{token}", token=token),     # nothing sent
            self.payload(token, "zz", good),                                # nonce shape
            self.payload(token, nonce, "zz"),                               # signature shape
            self.payload(token, "c" * 32, good),                            # never issued
            self.payload("no-such-token", nonce, good),                     # unknown link
        ]
        for resp in answers:
            self.assertEqual(resp.status_code, 404)
        self.assertEqual(len({a.body for a in answers}), 1)

    def test_unknown_link_and_no_key_material_are_404_at_stage_one(self):
        self.assertEqual(self.stage_one("nope-nope-nope").status_code, 404)
        token, _ = self.add()
        self.patch(main, "SCRIPT_ADMIN_TOKEN", "")
        self.patch(main, "LOADER_SECRET", "")
        self.assertEqual(self.stage_one(token).status_code, 404)

    def test_the_hit_counter_moves_on_the_payload_fetch_only(self):
        token, _ = self.add()
        _b, _t, nonce, secret = self.parse_stage(self.stage_one(token))
        self.assertEqual(self.script_rows()[0]["hits"], 0)
        self.payload(token, nonce, loader.sign(secret, token, nonce))
        self.assertEqual(self.script_rows()[0]["hits"], 1)
        self.payload(token, nonce, loader.sign(secret, token, nonce))      # replay
        self.assertEqual(self.script_rows()[0]["hits"], 1)

    def test_rotating_the_link_makes_the_old_one_stop_and_changes_the_secret(self):
        token, _ = self.add()
        old_secret = self.parse_stage(self.stage_one(token))[3]
        resp = self.rotate("v4")
        self.assertEqual(resp.status_code, 200)
        new_token = self.token_of(self.data(resp)["loadstring"])
        self.assertNotEqual(token, new_token)
        self.assertEqual(self.stage_one(token).status_code, 404)
        self.assertNotEqual(self.parse_stage(self.stage_one(new_token))[3], old_secret)

    def test_the_loader_key_survives_a_changed_admin_password_when_loader_secret_is_set(self):
        self.patch(main, "LOADER_SECRET", "L" * 40)
        token, _ = self.add()
        before = self.parse_stage(self.stage_one(token))[3]
        self.patch(main, "SCRIPT_ADMIN_TOKEN", "Z" * 40)
        self.assertEqual(self.parse_stage(self.stage_one(token))[3], before)

    def test_a_site_address_the_loader_cannot_carry_is_a_404_not_a_broken_loader(self):
        token, _ = self.add()
        self.patch(main, "SITE_URL", 'https://shop.example/"; os.execute("x")--')
        self.assertEqual(self.stage_one(token).status_code, 404)

    def test_a_large_script_comes_back_byte_for_byte_in_reasonable_time(self):
        raw = ("-- big\r\n" + "local x = 1\r\n" * 60000 + "print('é✓')\n").encode()
        token, _ = self.add("big", raw)
        started = time.monotonic()
        self.assertEqual(self.fetch(token).body, raw)
        self.assertLess(time.monotonic() - started, 5)


LUA_DRIVER = r'''
local stage = assert(io.open(arg[1], "rb")):read("a")
local expect_url_prefix = arg[2]
local out_path = arg[3]
local payload = assert(io.open(arg[4], "rb")):read("a")
local expect_sig = arg[5]
local calls = 0
game = {}
function game:HttpGet(url)
  calls = calls + 1
  if url:sub(1, #expect_url_prefix) ~= expect_url_prefix then error("unexpected url " .. url) end
  if not url:find("&s=" .. expect_sig, 1, true) then error("signature does not match " .. url) end
  return payload
end
_G.warn = function(...) io.stderr:write("warn: ", table.concat({...}, " "), "\n") end
local captured
local real_loadstring = loadstring or load
loadstring = function(src, ...)
  captured = src
  return function() end
end
local fn = assert(real_loadstring(stage))
fn()
local f = assert(io.open(out_path, "wb"))
f:write(captured or "")
f:close()
io.write(calls)
'''


@unittest.skipUnless(shutil.which("texlua"), "texlua (Lua 5.3) is not installed")
class RealLuaTests(ScriptCase):
    """Stage 1 run in a real Lua: it must request the signed URL, decrypt and hand the exact script to loadstring."""

    def run_stage(self, raw):
        token, _ = self.add("lua")      # the upload route wants non-empty UTF-8, the payload below is any bytes
        stage = self.stage_one(token)
        base, _t, nonce, secret = self.parse_stage(stage)
        sig = loader.sign(secret, token, nonce)
        payload = loader.encrypt(secret, nonce, raw)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "stage.lua").write_bytes(stage.body)
            (tmp / "driver.lua").write_text(LUA_DRIVER)
            (tmp / "payload.txt").write_bytes(payload)
            done = subprocess.run(
                ["texlua", str(tmp / "driver.lua"), str(tmp / "stage.lua"), f"{base}/p/{token}?n={nonce}",
                 str(tmp / "out.bin"), str(tmp / "payload.txt"), sig],
                capture_output=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr.decode())
            self.assertEqual(done.stdout.decode(), "1")
            return (tmp / "out.bin").read_bytes()

    def test_small_script(self):
        self.assertEqual(self.run_stage(SCRIPT.encode()), SCRIPT.encode())

    def test_empty_script_and_block_edges(self):
        for size in (0, 31, 32, 33):
            raw = bytes((i * 13 + 5) % 256 for i in range(size))
            self.assertEqual(self.run_stage(raw), raw, size)

    def test_binary_and_non_ascii_bytes_are_kept_exactly(self):
        raw = "print('é✓')\r\n".encode() + bytes(range(256))
        got = self.run_stage(raw)
        self.assertEqual(hashlib.sha256(got).hexdigest(), hashlib.sha256(raw).hexdigest())

    def test_a_large_script(self):
        raw = ("local x = 1\n" * 20000).encode()
        got = self.run_stage(raw)
        self.assertEqual(hashlib.sha256(got).hexdigest(), hashlib.sha256(raw).hexdigest())


if __name__ == "__main__":
    unittest.main()
