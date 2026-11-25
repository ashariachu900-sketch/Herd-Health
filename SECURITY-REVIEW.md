# CattleCare Security Review

## Summary

The review found two critical issues and two additional security findings. The critical authorization bypasses and sensitive-route exposures have been remediated. Production deployment still requires strong environment tokens and HTTPS.

| # | Severity | File | Lines | Vulnerability | Confidence |
|---|----------|------|-------|---------------|------------|
| 1 | 🔴 CRITICAL | server.py | 951-958 | Hard-coded `user/password` credentials grant requested admin or vet sessions | 10/10 |
| 2 | 🔴 CRITICAL | server.py | 1004-1142, 1147-1221, 1224-1265 | Public animal create, update, delete, and care-event mutations lack authorization | 10/10 |
| 3 | 🟠 HIGH | server.py | 783-827 | Public `/api/audit` and care-event routes expose sensitive operational and clinical data | 9/10 |
| 4 | 🟡 MEDIUM | server.py; docker-compose.yml | 78-84; 21 | Session cookies are not Secure by default in application and Compose configuration | 8/10 |

## Findings

### 1. Hard-coded demo credentials

`POST /api/session` accepts `username="user"` and `password="password"` and grants whichever role is supplied in `role`. Remove this branch and accept sessions only through `FARM_ADMIN_TOKEN` or `FARM_VET_TOKEN`.

```python
token = str(data.get("token") or "")

if token and ADMIN_TOKEN and hmac.compare_digest(token, ADMIN_TOKEN):
    role = "admin"
elif token and VET_TOKEN and hmac.compare_digest(token, VET_TOKEN):
    role = "vet"
else:
    role = None
```

### 2. Public data mutations

Animal creation, updates, deletion, and care-event creation do not call `check_auth(write=True)` or `check_vet_auth(write=True)`.

Add this check before processing bodies and database operations:

```python
if not self.check_auth(write=True):
    return
```

Use `check_vet_auth(write=True)` for veterinarian verification mutations.

### 3. Public audit and care-event reads

`GET /api/audit` and `GET /api/animals/<id>/events` execute and return database rows without an admin authorization check. Restrict both routes to authenticated administrators.

```python
if not self.check_auth():
    return
```

### 4. Insecure cookie default

`COOKIE_SECURE` defaults to `0` in the application and Docker Compose configuration. Use Secure cookies when serving HTTPS:

```text
COOKIE_SECURE=1
```

Keep HTTP-only local development explicitly separate from production.

## Remediation Status

- [x] Hard-coded demo credentials removed.
- [x] Animal and care-event mutations require an authenticated admin session or token plus CSRF for browser sessions.
- [x] `/api/audit` and `/api/animals/<id>/events` require admin authentication.
- [x] Docker Compose now defaults session cookies to Secure; local HTTP runs remain explicitly configurable.
- [x] Base64 photo uploads validate decoded size and format signatures.
- [x] Database connections enable foreign keys.
- [x] Replaced photo files are removed after successful updates.
- [x] The API/UI documentation and tests were updated to reflect protected mutations.
- [x] Audit sessions use server-assigned role actors instead of free-form operator names.
- [x] Failed-login throttling recognizes forwarded client IPs from trusted private or loopback proxies.
- [x] Vaccination events validate chronological date ordering.
- [x] Exports preserve and restores reconcile the animal tag sequence.

## Remediation Order

1. Remove the hard-coded demo credentials.
2. Re-enable authorization on all mutations.
3. Restrict audit and care-event reads.
4. Default session cookies to Secure in production.
5. Rotate both configured farm tokens after the bypass is removed.
