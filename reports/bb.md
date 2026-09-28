# Security audit — https://backgammon.free.nf

_2026-09-28T14:36:07+00:00 · tool v1.0_

> Audited https://backgammon.free.nf: 0 confirmed, 3 observed, 0 inconclusive, 3 cleared. Only 6 of 12 checks applied to this target; 6 had no surface to test. This is not a clean bill of health: an automated pass covers the checks listed here and nothing else, and the inconclusive ones were not silently dropped.

## Observed (3)

### CSP present but weakened

- **Check:** `security_headers`
- **Severity:** medium
- **Detail:** A CSP is sent, but it permits constructs that neutralise it: 'unsafe-inline'; 'unsafe-eval'; wasm-unsafe-eval'. An injection that lands in the page runs unchallenged.
- **Fix:** Remove the weakening tokens and use nonces or hashes for the scripts that genuinely need them.
- **Evidence:**
  - weakening: 'unsafe-inline' -- allows any script written into the page body or an attribute, which is what most injections produce
  - weakening: 'unsafe-eval' -- allows eval() and string-to-code, so a string that reaches a sink can become code
  - weakening: wasm-unsafe-eval' -- allows WebAssembly compilation, a code-loading path the policy otherwise closes
  - CSP as sent: default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' https://cdn.jsdelivr.net https://dlnzupvxtqozhczrenbn.supabase.co blob: https://www.googletagmanager.com; worker-src 'self' blob: https://dlnzupvxtqozhczrenbn.supabase.co; s

### Transport issue(s)

- **Check:** `transport`
- **Severity:** low
- **Detail:** plain http serves the site instead of redirecting to https.
- **Fix:** Set ssl_protocols TLSv1.2 TLSv1.3 and add a permanent redirect from port 80, plus Strict-Transport-Security.
- **Evidence:**
  - TLSv1.0: rejected
  - TLSv1.1: rejected
  - TLS 1.2/1.3 only
  - GET http://backgammon.free.nf/ -> 200, no redirect

### Information disclosed

- **Check:** `information_disclosure`
- **Severity:** low
- **Detail:** Server banner discloses 'openresty'. Version banners let an attacker match known vulnerabilities without probing.
- **Fix:** server_tokens off; remove X-Powered-At the proxy; keep debug output out of production.
- **Evidence:**
  - Server banner discloses 'openresty'

## Not Applicable (6)

### Server-side template injection

- **Check:** `ssti`
- **Severity:** info
- **Detail:** Needs a server-side template engine rendering user input. This target serves pre-built static files, so there is no template to inject into.

### LDAP injection

- **Check:** `ldap_injection`
- **Severity:** info
- **Detail:** Needs an LDAP directory in the request path. None was observed.

### XML external entity

- **Check:** `xxe`
- **Severity:** info
- **Detail:** Needs an XML parser accepting user-supplied XML. No such endpoint was found.

### Insecure deserialization

- **Check:** `insecure_deserialization`
- **Severity:** info
- **Detail:** Needs an object stream or stateful serialisation format crossing the trust boundary. A static bundle has none.

### Command injection

- **Check:** `command_injection`
- **Severity:** info
- **Detail:** Needs a server-side process spawning a shell with request data. No such surface was found.

### Remote file inclusion

- **Check:** `rfi`
- **Severity:** info
- **Detail:** Needs a server-side include or dynamic import. None found.

## Cleared (3)

### No secret files found

- **Check:** `sensitive_data_exposure`
- **Severity:** info
- **Detail:** Probed 17 well-known paths. 4 closed the connection without a response, which is a deny rule doing its job: /.env, /.git/config, /.htaccess, /wp-config.php.bak. The rest returned no matching content. This shows those files are not served; it does not prove no other path exposes them.
- **Evidence:**
  - no secret pattern in any 200 response across 17 paths

### No permissive CORS

- **Check:** `cors`
- **Severity:** info
- **Detail:** A cross-origin preflight from an unrelated origin was refused, which is the correct default for a site that serves its own API from its own origin.
- **Evidence:**
  - no Access-Control-Allow-Origin in the preflight response

### No cookies set

- **Check:** `cookie_flags`
- **Severity:** info
- **Detail:** The response sets no cookies, so there are no flags to check. Note that this is also what a site with no session looks like.
- **Evidence:**
  - no Set-Cookie in the response
