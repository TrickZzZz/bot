"""Two-stage hosted loader: what /l/<token> and /p/<token> in app/main.py serve.

Stage 1 is a short Lua program. The link people paste, loadstring(game:HttpGet(".../l/<token>"))(),
now returns it instead of the script. Stage 1 carries a one-time number (a nonce) and a secret for
this link, signs a request for the script, receives it encrypted, decrypts it in memory and runs it.
The user sees nothing and types no key.

What this gives: a copy of the link's text, a browser, curl or a saved network capture no longer
holds the script, a captured request works once, and the script only travels encrypted.
What it does not give: stage 1 holds the link's secret, so anyone who runs it (an executor that
hooks loadstring, say) can still read the script after it is decrypted. The Luraph layer on the
script is what protects it from there.

The wire format, shared with the Lua in STAGE_ONE (change both or neither):
  secret   = first 32 hex characters of HMAC-SHA256(master, b"link|" + token)
  request  = /p/<token>?n=<nonce>&s=<HMAC-SHA256(secret, "p|<token>|<nonce>") in hex>
  key      = SHA-256("<secret>|<nonce>|k")
  stream   = SHA-256(key + 4-byte big-endian block number), block 0, 1, 2 ... (32 bytes each)
  payload  = base64( XOR(stream, le32(length) + b"DCv1" + script bytes) )
"""
import base64
import hashlib
import hmac
import re
import secrets
import struct
import threading
import time

NONCE_TTL = 120          # seconds a stage 1 may wait before it asks for the script
NONCE_CAP = 20_000       # nonces kept at once; the oldest go first (memory bound, not a visitor limit)
MIN_SECRET_LEN = 24      # a LOADER_SECRET or admin password shorter than this is not used as key material
MAGIC = b"DCv1"
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
SIG_RE = re.compile(r"^[0-9a-f]{64}$")
BASE_RE = re.compile(r"^https?://[A-Za-z0-9.\-:\[\]]+(/[A-Za-z0-9._\-/]*)?$")


class NonceStore:
    """One-time numbers. take() answers True once per issued nonce, for its own token, before it expires."""

    def __init__(self, ttl: float = NONCE_TTL, cap: int = NONCE_CAP, clock=time.monotonic):
        self._ttl, self._cap, self._clock = ttl, cap, clock
        self._items: dict = {}
        self._lock = threading.Lock()

    def issue(self, token: str) -> str:
        nonce = secrets.token_hex(16)
        with self._lock:
            if len(self._items) >= self._cap:
                now = self._clock()
                self._items = {n: v for n, v in self._items.items() if v[1] >= now}
                if len(self._items) >= self._cap:        # still full: forget the older half
                    keep = list(self._items.items())[len(self._items) // 2:]
                    self._items = dict(keep)
            self._items[nonce] = (token, self._clock() + self._ttl)
        return nonce

    def take(self, token: str, nonce: str) -> bool:
        with self._lock:
            entry = self._items.pop(nonce, None)       # popped either way: a nonce is never tried twice
        return entry is not None and entry[0] == token and entry[1] >= self._clock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def master_key(loader_secret: str, admin_token: str):
    """Key material for every link: LOADER_SECRET when it is long enough, otherwise the admin password
    (so the feature works with nothing new to set). None when neither is usable."""
    for source, label in ((loader_secret, b"hosted-loader-v1|env"), (admin_token, b"hosted-loader-v1|admin")):
        if source and len(source) >= MIN_SECRET_LEN:
            return hmac.new(source.encode(), label, hashlib.sha256).digest()
    return None


def link_secret(master: bytes, token: str) -> str:
    """The secret stage 1 carries for this link. A new token (the admin page's "New link") gives a new secret."""
    return hmac.new(master, b"link|" + token.encode(), hashlib.sha256).hexdigest()[:32]


def sign(secret: str, token: str, nonce: str) -> str:
    return hmac.new(secret.encode(), f"p|{token}|{nonce}".encode(), hashlib.sha256).hexdigest()


def signature_ok(secret: str, token: str, nonce: str, given: str) -> bool:
    return hmac.compare_digest(given, sign(secret, token, nonce))


def _stream(key: bytes, length: int) -> bytes:
    blocks = (length + 31) // 32
    return b"".join(hashlib.sha256(key + struct.pack(">I", i)).digest() for i in range(blocks))[:length]


def _xor(data: bytes, stream: bytes) -> bytes:
    if not data:
        return b""
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(len(data), "big")


def encrypt(secret: str, nonce: str, script: bytes) -> bytes:
    """The script as the payload text /p/<token> sends (base64 of the encrypted, framed bytes)."""
    framed = struct.pack("<I", len(script)) + MAGIC + script
    key = hashlib.sha256(f"{secret}|{nonce}|k".encode()).digest()
    return base64.b64encode(_xor(framed, _stream(key, len(framed))))


def decrypt(secret: str, nonce: str, payload: bytes):
    """What stage 1 does, in Python (the tests use it to check the server side). None for a bad payload."""
    try:
        raw = base64.b64decode(payload, validate=True)
    except ValueError:
        return None
    key = hashlib.sha256(f"{secret}|{nonce}|k".encode()).digest()
    framed = _xor(raw, _stream(key, len(raw)))
    if len(framed) < 8 or framed[4:8] != MAGIC or struct.unpack("<I", framed[:4])[0] != len(framed) - 8:
        return None
    return framed[8:]


STAGE_ONE = r'''-- hosted loader, stage 1: asks the site for the script with a signed one-time request, decrypts it here and runs it
local BASE, TOKEN, NONCE, SECRET = "__BASE__", "__TOKEN__", "__NONCE__", "__SECRET__"

local function fail(msg)
    warn("[loader] " .. tostring(msg))
end

local b32 = bit32
if not b32 then
    fail("this executor has no bit32 library, so the script cannot be decrypted")
    return
end
local band, bxor, bnot, rshift, rrotate = b32.band, b32.bxor, b32.bnot, b32.rshift, b32.rrotate

local MOD = 4294967296
local K = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
}

-- SHA-256 of a string, as 32 raw bytes
local function sha256(msg)
    local h0, h1, h2, h3 = 0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a
    local h4, h5, h6, h7 = 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
    local len = #msg
    local bits = len * 8
    msg = msg .. "\128" .. string.rep("\0", (55 - len) % 64)
        .. string.char(0, 0, 0, 0, band(rshift(bits, 24), 255), band(rshift(bits, 16), 255), band(rshift(bits, 8), 255), band(bits, 255))
    local w = {}
    for chunk = 1, #msg, 64 do
        for i = 0, 15 do
            local a, b, c, d = string.byte(msg, chunk + i * 4, chunk + i * 4 + 3)
            w[i] = ((a * 256 + b) * 256 + c) * 256 + d
        end
        for i = 16, 63 do
            local x, y = w[i - 15], w[i - 2]
            local s0 = bxor(rrotate(x, 7), rrotate(x, 18), rshift(x, 3))
            local s1 = bxor(rrotate(y, 17), rrotate(y, 19), rshift(y, 10))
            w[i] = (w[i - 16] + s0 + w[i - 7] + s1) % MOD
        end
        local a, b, c, d, e, f, g, h = h0, h1, h2, h3, h4, h5, h6, h7
        for i = 0, 63 do
            local S1 = bxor(rrotate(e, 6), rrotate(e, 11), rrotate(e, 25))
            local ch = bxor(band(e, f), band(bnot(e), g))
            local t1 = (h + S1 + ch + K[i + 1] + w[i]) % MOD
            local S0 = bxor(rrotate(a, 2), rrotate(a, 13), rrotate(a, 22))
            local maj = bxor(band(a, b), band(a, c), band(b, c))
            local t2 = (S0 + maj) % MOD
            h, g, f, e, d, c, b, a = g, f, e, (d + t1) % MOD, c, b, a, (t1 + t2) % MOD
        end
        h0, h1, h2, h3 = (h0 + a) % MOD, (h1 + b) % MOD, (h2 + c) % MOD, (h3 + d) % MOD
        h4, h5, h6, h7 = (h4 + e) % MOD, (h5 + f) % MOD, (h6 + g) % MOD, (h7 + h) % MOD
    end
    local out = {}
    for _, v in ipairs({ h0, h1, h2, h3, h4, h5, h6, h7 }) do
        out[#out + 1] = string.char(rshift(v, 24), band(rshift(v, 16), 255), band(rshift(v, 8), 255), band(v, 255))
    end
    return table.concat(out)
end

local function tohex(s)
    return (s:gsub(".", function(c) return string.format("%02x", string.byte(c)) end))
end

local function hmac_hex(key, msg)
    if #key > 64 then key = sha256(key) end
    key = key .. string.rep("\0", 64 - #key)
    local inner, outer = {}, {}
    for i = 1, 64 do
        local kb = string.byte(key, i)
        inner[i] = string.char(bxor(kb, 0x36))
        outer[i] = string.char(bxor(kb, 0x5c))
    end
    return tohex(sha256(table.concat(outer) .. sha256(table.concat(inner) .. msg)))
end

-- a long job gives the executor a turn now and then, so it is never seen as stuck
local function pause(counter, every)
    if task and task.wait and counter % every == 0 then task.wait() end
end

local B64 = {}
do
    local alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    for i = 1, 64 do B64[string.byte(alphabet, i)] = i - 1 end
end

local function b64decode(s)
    s = s:gsub("%s", "")
    local pad = 0
    if s:sub(-2) == "==" then pad = 2 elseif s:sub(-1) == "=" then pad = 1 end
    local out, n = {}, 0
    for i = 1, #s, 4 do
        local c1, c2, c3, c4 = string.byte(s, i, i + 3)
        local v = ((B64[c1] * 64 + B64[c2]) * 64 + (B64[c3] or 0)) * 64 + (B64[c4] or 0)
        n = n + 1
        out[n] = string.char(rshift(v, 16), band(rshift(v, 8), 255), band(v, 255))
        pause(n, 4096)
    end
    local joined = table.concat(out)
    return joined:sub(1, #joined - pad)
end

local function decrypt(ct, secret, nonce)
    local key = sha256(secret .. "|" .. nonce .. "|k")
    local total = #ct
    local blocks = math.floor((total + 31) / 32)
    local out = {}
    for blk = 0, blocks - 1 do
        local ks = sha256(key .. string.char(rshift(blk, 24), band(rshift(blk, 16), 255), band(rshift(blk, 8), 255), band(blk, 255)))
        local first = blk * 32 + 1
        local last = math.min(first + 31, total)
        local piece = {}
        for j = first, last do
            piece[#piece + 1] = string.char(bxor(string.byte(ct, j), string.byte(ks, j - first + 1)))
        end
        out[blk + 1] = table.concat(piece)
        pause(blk + 1, 64)
    end
    return table.concat(out)
end

-- the script comes framed as: 4-byte little-endian length, "DCv1", then the script
local function unframe(plain)
    if #plain < 8 or plain:sub(5, 8) ~= "DCv1" then return nil end
    local a, b, c, d = string.byte(plain, 1, 4)
    if a + b * 256 + c * 65536 + d * 16777216 ~= #plain - 8 then return nil end
    return plain:sub(9)
end

local function run()
    local sig = hmac_hex(SECRET, "p|" .. TOKEN .. "|" .. NONCE)
    local url = BASE .. "/p/" .. TOKEN .. "?n=" .. NONCE .. "&s=" .. sig
    local okGet, body = pcall(function() return game:HttpGet(url) end)
    if not okGet or type(body) ~= "string" or body == "" then
        fail("the site did not send the script (" .. tostring(body) .. ")")
        return
    end
    local code = unframe(decrypt(b64decode(body), SECRET, NONCE))
    if not code then
        fail("the script could not be decrypted")
        return
    end
    local fn, err = loadstring(code)
    if not fn then
        fail("the script did not load: " .. tostring(err))
        return
    end
    fn()
end

local function start()
    local ok, err = pcall(run)
    if not ok then fail(err) end
end

if task and task.spawn then task.spawn(start) else start() end
'''


def stage_one(base: str, token: str, nonce: str, secret: str) -> str:
    """Stage 1 for one request. Everything put into it is checked first, so it can never break out of its quotes."""
    if not BASE_RE.fullmatch(base) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token) \
            or not NONCE_RE.fullmatch(nonce) or not re.fullmatch(r"[0-9a-f]{32}", secret):
        raise ValueError("stage 1 inputs are not in the expected shape")
    return (STAGE_ONE.replace("__BASE__", base).replace("__TOKEN__", token)
            .replace("__NONCE__", nonce).replace("__SECRET__", secret))
