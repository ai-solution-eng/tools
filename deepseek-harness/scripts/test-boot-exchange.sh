#!/usr/bin/env bash
# Integration-test the auth validator's /boot token exchange locally.
#
#   scripts/test-boot-exchange.sh [image]
#
# Runs the RENDERED auth_validator.mjs (extracted from the chart) inside the
# given image (default: the baked user image — the actual pod runtime), with:
#   - a stub dsh on 127.0.0.1:3080 that mimics the launch-token exchange AND
#     enforces dsh's trusted-host check (the exchange only succeeds when the
#     request carries the public Host — a loopback Host is refused, which is
#     what broke the first server-side-exchange attempt in production)
#   - a boot-state file carrying dsh's captured launch URL
# and asserts:
#   1. valid session + public Host -> 302 into /<slug>/ with dsh's cookie
#   2. loopback Host -> dsh refuses -> 503 (loop-proof: NO redirect)
#   3. invalid cookie -> 401
#   4. validly-signed cookie for another slug -> 401 (tenant isolation)
set -euo pipefail

cd "$(dirname "$0")/.."

PLATFORM="linux/amd64"
DSH_VERSION="$(grep -E '^  version:' values.yaml | head -1 | sed -e 's/^  version:[[:space:]]*//' -e 's/^"//' -e 's/"[[:space:]]*$//')"
IMAGE="${1:-ghcr.io/ai-solution-eng/deepseek-harness:${DSH_VERSION}}"

DOCKER="$(command -v docker || true)"
if [ -z "$DOCKER" ]; then
  for candidate in /usr/local/bin/docker /opt/homebrew/bin/docker "$HOME/.docker/bin/docker"; do
    if [ -x "$candidate" ]; then DOCKER="$candidate"; break; fi
  done
fi
[ -n "$DOCKER" ] || { echo "ERROR: docker CLI not found" >&2; exit 1; }
export PATH="$(dirname "$DOCKER"):/usr/local/bin:/opt/homebrew/bin:$PATH"

NODE_BIN="$(command -v node || true)"
if [ -z "$NODE_BIN" ]; then
  for candidate in /opt/homebrew/bin/node /usr/local/bin/node; do
    if [ -x "$candidate" ]; then NODE_BIN="$candidate"; break; fi
  done
fi
[ -n "$NODE_BIN" ] || { echo "ERROR: node CLI not found" >&2; exit 1; }

echo "==> rendering chart and extracting auth_validator.mjs"
/opt/homebrew/bin/helm template dsh . -f values-g2.yaml > .tmp-boottest-render.yaml
awk '
/^  auth_validator\.mjs: \|/{k="v";next}
/^  [a-zA-Z0-9_.-]+:/{k=""}
/^---/{k=""}
k=="v"{print substr($0,5)}
' .tmp-boottest-render.yaml > .tmp-boottest-validator.mjs
rm -f .tmp-boottest-render.yaml
[ -s .tmp-boottest-validator.mjs ] || { echo "ERROR: extraction failed" >&2; exit 1; }

echo "==> running validator + stub dsh inside $IMAGE"
"$DOCKER" run --rm --platform "$PLATFORM" \
  -v "$PWD/.tmp-boottest-validator.mjs:/t/validator.mjs:ro" \
  "$IMAGE" node --input-type=module -e '
import http from "node:http"
import crypto from "node:crypto"
import fs from "node:fs"
import assert from "node:assert"

const TRUSTED_HOST = "deepseekharness.test"   // what nginx forwards ($host)
const SECRET = crypto.createHmac("sha256", "test-secret").update("test-user").digest("hex")

// --- stub dsh: token exchange WITH dsh trusted-host enforcement ----------
// Real dsh answers the exchange with 303 See Other (+ its session cookie).
// TOKEN123: cookie on the first hop (observed production behavior).
// TOKEN456: cookie only on the SECOND hop (chain-following coverage).
const stub = http.createServer((req, res) => {
  if (req.url.startsWith("/?token=TOKEN123")) {
    if (req.headers.host !== TRUSTED_HOST) {
      res.writeHead(403, { "content-type": "application/json" })
      res.end(JSON.stringify({ error: "host not trusted" }))
      return
    }
    res.writeHead(303, {
      "set-cookie": ["dsh_web_session=launch-ok; Path=/; HttpOnly; SameSite=Lax"],
      location: "/",
    })
    res.end()
    return
  }
  if (req.url.startsWith("/?token=TOKEN456")) {
    if (req.headers.host !== TRUSTED_HOST) {
      res.writeHead(403, { "content-type": "application/json" })
      res.end(JSON.stringify({ error: "host not trusted" }))
      return
    }
    res.writeHead(303, { location: "/hop2" })   // no cookie yet — chain continues
    res.end()
    return
  }
  if (req.url.startsWith("/hop2")) {
    if (req.headers.host !== TRUSTED_HOST) {
      res.writeHead(403, { "content-type": "application/json" })
      res.end(JSON.stringify({ error: "host not trusted" }))
      return
    }
    res.writeHead(302, {
      "set-cookie": ["dsh_web_session=hop2-ok; Path=/; HttpOnly; SameSite=Lax"],
      location: "/",
    })
    res.end()
    return
  }
  res.writeHead(404)
  res.end()
})
await new Promise((ok) => stub.listen(3080, "127.0.0.1", ok))

// --- boot state ----------------------------------------------------------
fs.mkdirSync("/var/dsh/state", { recursive: true })
fs.writeFileSync("/var/dsh/state/dsh-boot-url", "http://127.0.0.1:3080/?token=TOKEN123")

// --- run the RENDERED validator in-process -------------------------------
process.env.AUTH_VALIDATOR_PORT = "7684"
process.env.SESSION_KEY = SECRET
process.env.USER_SLUG = "test-user"
process.env.DSH_BOOT_STATE_FILE = "/var/dsh/state/dsh-boot-url"
await import("/t/validator.mjs")
await new Promise((ok) => setTimeout(ok, 300))

const COOKIE = "dsh_sess"
function sessionCookie(slug) {
  const payload = Buffer.from(JSON.stringify({ u: slug, slug, exp: Math.floor(Date.now() / 1000) + 60 })).toString("base64url")
  const sig = crypto.createHmac("sha256", SECRET).update(payload).digest("hex")
  return `${COOKIE}=${payload}.${sig}`
}
// http.request (not fetch) so the Host header can be presented like nginx does
function get(path, cookie, host) {
  return new Promise((resolve, reject) => {
    const r = http.request(
      { host: "127.0.0.1", port: 7684, path, method: "GET", headers: { cookie: cookie || "", host: host || TRUSTED_HOST } },
      (res) => {
        const chunks = []
        res.on("data", (c) => chunks.push(c))
        res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString() }))
      },
    )
    r.on("error", reject)
    r.end()
  })
}

// 1. valid session + public Host -> straight into the unit, cookie forwarded
//    (dsh answered 303 — must NOT be status-matched to 302)
const r1 = await get("/boot", sessionCookie("test-user"))
assert.strictEqual(r1.status, 302, "boot should 302")
assert.strictEqual(r1.headers.location, "/test-user/", "must land INSIDE the unit (prefixed), not /?token=")
assert.ok((r1.headers["set-cookie"] || []).some((c) => c.includes("dsh_web_session=launch-ok")), "dsh session cookie must be forwarded")
console.log("  PASS: 303-with-cookie exchange -> 302 /test-user/, cookie forwarded")

// 2. cookie arriving on a LATER hop of the exchange chain -> still succeeds
fs.writeFileSync("/var/dsh/state/dsh-boot-url", "http://127.0.0.1:3080/?token=TOKEN456")
const r5 = await get("/boot", sessionCookie("test-user"))
assert.strictEqual(r5.status, 302, "chain exchange should 302")
assert.strictEqual(r5.headers.location, "/test-user/", "chain exchange must land inside the unit")
assert.ok((r5.headers["set-cookie"] || []).some((c) => c.includes("dsh_web_session=hop2-ok")), "cookie from the second hop must be forwarded")
console.log("  PASS: cookie on second hop -> followed and forwarded")

// restore the primary boot URL for the failure scenarios
fs.writeFileSync("/var/dsh/state/dsh-boot-url", "http://127.0.0.1:3080/?token=TOKEN123")

// 2. loopback Host -> dsh refuses the exchange -> 503, NO redirect (loop-proof)
const r2 = await get("/boot", sessionCookie("test-user"), "127.0.0.1:7684")
assert.strictEqual(r2.status, 503, "untrusted-host exchange must fail visibly, not redirect-loop")
assert.ok(!r2.headers.location, "must NOT redirect when the exchange yields no cookie")
console.log("  PASS: loopback Host -> 503 (loop-proof, no redirect)")

// 3. invalid cookie -> 401
const r3 = await get("/boot", `${COOKIE}=bogus.sig`)
assert.strictEqual(r3.status, 401, "invalid cookie must 401")
console.log("  PASS: invalid session -> 401")

// 4. validly-signed cookie for a DIFFERENT slug -> 401 (tenant isolation)
const payload4 = Buffer.from(JSON.stringify({ u: "other-user", slug: "other-user", exp: Math.floor(Date.now() / 1000) + 60 })).toString("base64url")
const sig4 = crypto.createHmac("sha256", SECRET).update(payload4).digest("hex")
const r4 = await get("/boot", `${COOKIE}=${payload4}.${sig4}`)
assert.strictEqual(r4.status, 401, "cross-slug cookie must 401")
console.log("  PASS: cookie signed for another slug -> 401")

console.log("BOOT EXCHANGE TESTS PASSED")
'
STATUS=$?
rm -f .tmp-boottest-validator.mjs
exit $STATUS
