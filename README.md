# Sewa Setu — Old Age Pension Portal

Department of Social Welfare, Government of Purvanchal. Maintained in-house.

## Running

```
ADMIN_PASSWORD=pick-something docker compose up --build
```

- Portal: http://localhost:8000
- Status portal: http://localhost:8000/status (mobile number + password; the
  password is the applicant's date of birth as DDMMYYYY)
- Departmental login: http://localhost:8000/admin (user `admin`, password from
  `ADMIN_PASSWORD`)
- SMS gateway console (simulated telecom): http://localhost:8000/__gateway/ —
  OTPs and notifications appear here once "delivered" (1–4 s carrier latency).
  The gateway's inbox is also readable as JSON at
  `/__gateway/api/messages?to=<mobile>`.
- Database: Postgres, loaded from `seed/seed.sql` on first boot (~100k applications).

## Deploying on a server

Set `PUBLIC_BASE_URL` to the address people will use (for example
`https://sewasetu.example.in`). It is stamped into sign-in tokens and the discovery documents
described below, so a wrong value means other applications cannot find the sign-in page. Put
the portal behind HTTPS; some applications refuse to sign in over plain HTTP.

```
PUBLIC_BASE_URL=https://sewasetu.example.in ADMIN_PASSWORD=pick-something docker compose up --build -d
```

## What the portal does

Citizen flow: mobile → OTP → personal details → address → bank details →
age-proof upload → preview and declaration → application number. A nightly
job (`scripts/deemed_approval.py`, via cron in the `scheduler` container)
deems approved any application still pending after the 15-day statutory SLA.

Rules enforced at submission, in `validate_application()` in `app/app.py`:
applicant is 60 years of age or above; required fields and an age-proof
document are present; the scheme window is open; and the mobile number holds
no other active (pending / approved / deemed-approved) application. A pending
application can be withdrawn from the status portal, after which the same
mobile may apply again.

Departmental users see a dashboard, list and filter applications, approve or
reject pending ones, and see the audit trail per application.

## Layout

```
app/          Flask application, templates, config, nightly job
seed/         seed.sql — production data snapshot
smsgw/        simulated SMS gateway
```

## Letting other applications act for a citizen or an officer

Sewa Setu is also an **OAuth 2.1 authorization server**. A third-party application can act
for a citizen (scope `citizen`) or for a block officer (scope `officer`) once that person
signs in on the portal and approves it. Citizens sign in by mobile and OTP, officers with the
departmental login. The application never handles the OTP or the password.

- Discovery: `/.well-known/oauth-authorization-server` (RFC 8414)
- Registration: `/oauth/register` (RFC 7591), or a client ID metadata document URL as `client_id`
- Authorization: `/oauth/authorize` (authorization code with PKCE S256; the `resource` the
  application asks for becomes the token's audience, RFC 8707)
- Tokens: `/oauth/token`. Access tokens are RS256 JWTs. A sign-in is a **session with an
  absolute end**: 2 hours for a citizen, 8 hours for an officer, counted from the consent.
  Access tokens are short (20 / 30 minutes) and refresh tokens rotate within the session but
  never extend it; after it ends the person signs in again. Portals are used on shared phones
  and kiosks.
- Sign-out: `/oauth/revoke` (RFC 7009). People can also end any sign-in from the status
  portal (**Connected applications**) or, for officers, the admin dashboard.
- Keys: `/.well-known/jwks.json`

A service that receives one of these tokens verifies it with the JWKS, checks `iss` is this
portal, checks `aud` is its own canonical URL, and reads `scope`, `role` and `sub`
(`citizen:<mobile>` or `officer:<username>`).

Long-lived **program tokens** for unattended use (role officer) are issued by an
administrator at `/admin/tokens`.

## AI assistant MCP endpoints

The project now includes two separately deployed streamable-HTTP MCP services:

- Citizen endpoint: `http://localhost:8101/mcp` (citizen tools only)
- Block officer endpoint: `http://localhost:8102/mcp` (officer tools only)
- Evaluation dashboard: `http://localhost:8000/eval-dashboard`

Start the full stack with `docker compose up --build`. For a public deployment, set
`PUBLIC_BASE_URL`, `CITIZEN_MCP_PUBLIC_URL` (canonical endpoint URL ending in `/mcp`),
`OFFICER_MCP_PUBLIC_URL`, `ADMIN_PASSWORD`, `SECRET_KEY`, and `DB_PASSWORD` in the
environment before starting the services. Put the portal and both MCP endpoints behind
HTTPS. Do not use the development defaults in a public deployment.

OAuth authorization must request the endpoint's canonical URL as the `resource` value,
so the access token audience matches the service. The services validate the portal's
RS256 token signature using its JWKS, plus issuer, audience, expiry, role, scope, and
subject shape. Citizen identity is taken only from the signed token; a tool caller cannot
supply another mobile number. The officer endpoint is a separate process and exposes no
citizen tools. Its jurisdiction defaults to `Sonari` (least privilege); set
`MCP_ALLOWED_BLOCKS` to a comma-separated subset of the portal's supported blocks when
widening jurisdiction is appropriate.

Citizen application submission calls the portal's shared `validate_application()` and
`create_application()` functions. The applicant must first upload age proof through the
official portal; the MCP tool accepts only a real file path under the portal's configured
upload directory. It does not fabricate a document, approve an application, or bypass the
scheme deadline. Officer decisions require a reason and are limited to pending cases in
the configured block subset; the portal status and audit log are updated transactionally.

### Evaluation runner

`evals/personas.json` describes the test personas and expected database/security outcomes.
`evals/run_evals.py` is a real MCP-client smoke runner. After obtaining portal-authorized
access tokens with the correct resource audiences, run:

```bash
CITIZEN_TOKEN='...' OFFICER_TOKEN='...' \
CITIZEN_MCP_URL='http://localhost:8101/mcp' \
OFFICER_MCP_URL='http://localhost:8102/mcp' \
python evals/run_evals.py
```

The runner exercises eligibility, own-status lookup, tool separation, officer queue
jurisdiction, and cross-role token rejection, then writes `evals/results.json`, which is
shown by `/eval-dashboard`. It intentionally does not submit a real citizen application
or make an officer decision. The dashboard says `NOT_RUN` until an actual run records
results; never report unexecuted scenarios as passed.
