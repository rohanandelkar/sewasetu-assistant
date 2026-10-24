"""Separate, OAuth-protected Sewa Setu MCP endpoints.

Run with MCP_ROLE=citizen or MCP_ROLE=officer. All writes use the portal's
existing validation and application creation functions; the portal remains the
system of record.
"""
import os
import sys
import json
import logging
from contextvars import ContextVar
from functools import lru_cache

import jwt
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
import uvicorn
from mcp.server.fastmcp import FastMCP, Context

ROLE = os.environ.get("MCP_ROLE", "citizen").strip().lower()
if ROLE not in {"citizen", "officer"}:
    raise RuntimeError("MCP_ROLE must be citizen or officer")

PORT = int(os.environ.get("PORT", "8101" if ROLE == "citizen" else "8102"))
ISSUER = os.environ.get("PORTAL_ISSUER", "http://localhost:8000").rstrip("/")
JWKS_URL = os.environ.get("PORTAL_JWKS_URL", "http://app:8000/.well-known/jwks.json")
AUDIENCE = os.environ.get(
    "MCP_AUDIENCE",
    os.environ.get("CITIZEN_MCP_PUBLIC_URL", "http://localhost:8101/mcp") if ROLE == "citizen"
    else os.environ.get("OFFICER_MCP_PUBLIC_URL", "http://localhost:8102/mcp"),
)
ALLOWED_BLOCKS = tuple(x.strip() for x in os.environ.get(
    "MCP_ALLOWED_BLOCKS", "Sonari"
).split(",") if x.strip())
DB = {
    "host": os.environ.get("DB_HOST", "db"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "sewasetu"),
    "user": os.environ.get("DB_USER", "sewasetu"),
    "password": os.environ.get("DB_PASSWORD", "sewasetu123"),
}
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("sewasetu-mcp")

# Load the portal module so application creation uses the exact same validation
# and insertion functions as the browser form. Import path is supplied by Docker.
sys.path.insert(0, "/srv/portal_app")
import app as portal  # noqa: E402

# Fail closed for typos or unsupported block names in deployment configuration.
ALLOWED_BLOCKS = tuple(block for block in ALLOWED_BLOCKS if block in portal.BLOCKS)

AUTH_CLAIMS = ContextVar("sewasetu_auth_claims", default=None)

mcp = FastMCP(
    "Sewa Setu Citizen Assistant" if ROLE == "citizen" else "Sewa Setu Block Officer",
    instructions=(
        "Help citizens check eligibility, status, and submit valid applications. Never promise or grant a pension."
        if ROLE == "citizen" else
        "Help authorized block officers review pending applications and record reasoned decisions."
    ),
)

@lru_cache(maxsize=1)
def jwks_client():
    return jwt.PyJWKClient(JWKS_URL, cache_keys=True, lifespan=300)


def claims_for_token(token: str):
    try:
        key = jwks_client().get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER)
        token_kind = claims.get("kind", "access")
        if token_kind not in {"access", "program"} or (token_kind == "program" and ROLE != "officer"):
            return None
        expected_role = ROLE
        if claims.get("role") != expected_role:
            return None
        scopes = set((claims.get("scope") or "").split())
        if expected_role not in scopes:
            return None
        sub = claims.get("sub", "")
        if expected_role == "citizen" and not sub.startswith("citizen:"):
            return None
        if expected_role == "officer" and not sub.startswith("officer:"):
            return None
        return claims
    except Exception as exc:
        log.info("token rejected: %s", type(exc).__name__)
        return None


class BearerAuthASGI:
    """Require and verify portal access tokens on all MCP HTTP requests."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode("latin1")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        claims = claims_for_token(token) if token else None
        if not claims:
            body = json.dumps({"error": "unauthorized", "message": "A valid portal access token for this MCP endpoint is required."}).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b'Bearer error="invalid_token"'),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        token_handle = AUTH_CLAIMS.set(claims)
        try:
            await self.app(scope, receive, send)
        finally:
            AUTH_CLAIMS.reset(token_handle)


def get_claims(ctx: Context = None):
    """Return claims verified by the ASGI middleware; never trust tool arguments."""
    claims = AUTH_CLAIMS.get()
    if claims:
        return claims
    raise ValueError("Authenticated identity is unavailable; reconnect to the portal.")


def db_connect():
    return psycopg2.connect(**DB)


def as_iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value

if ROLE == "citizen":
    @mcp.tool()
    def check_eligibility(date_of_birth: str, block: str) -> dict:
        """Check age and scheme-window rules. DOB must be DD/MM/YYYY; this does not submit an application."""
        try:
            dob = portal.parse_dob(date_of_birth)
        except Exception:
            dob = None
        if not dob:
            return {"eligible": False, "reasons": ["Date of birth must be DD/MM/YYYY."]}
        reasons = []
        age = portal.age_on(dob, portal.now_ist().date())
        if age < portal.MIN_AGE:
            reasons.append(f"Applicant must be at least {portal.MIN_AGE}; calculated age is {age}.")
        if block not in portal.BLOCKS:
            reasons.append("Block must be one of the blocks served by this portal.")
        if portal.now_ist() > portal.SCHEME_DEADLINE:
            reasons.append("The application window has closed.")
        return {"eligible": not reasons, "age_years": age, "block": block,
                "scheme_deadline": portal.SCHEME_DEADLINE.isoformat(), "reasons": reasons,
                "notice": "Eligibility check only; the portal makes the final decision."}

    @mcp.tool()
    def get_my_application_status(ctx: Context) -> dict:
        """Return only the authenticated citizen's current or most recent application status."""
        claims = get_claims(ctx)
        mobile = claims["sub"].split(":", 1)[1]
        conn = db_connect()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT application_no, applicant_name, village, block, status, submitted_at, decided_at, decided_by FROM applications WHERE mobile=%s ORDER BY submitted_at DESC LIMIT 1", (mobile,))
                row = cur.fetchone()
            if not row:
                return {"found": False, "message": "No application was found for the signed-in mobile number."}
            return {"found": True, **{k: as_iso(v) for k, v in row.items()},
                    "notice": "Status is read from the official Sewa Setu portal."}
        finally:
            conn.close()

    @mcp.tool()
    def submit_application(ctx: Context, applicant_name: str, date_of_birth: str, village: str,
                           block: str, bank_account: str, ifsc: str,
                           age_proof_document_path: str, gender: str = "",
                           marital_status: str = "", husband_name: str = "") -> dict:
        """Submit an application for the authenticated citizen. The age-proof file must already exist in the portal's upload directory; this tool never fabricates proof."""
        claims = get_claims(ctx)
        mobile = claims["sub"].split(":", 1)[1]
        # Accept only an existing file under the configured upload directory.
        upload_root = os.path.realpath(portal.UPLOAD_DIR)
        doc_path = os.path.realpath(age_proof_document_path or "")
        if not doc_path.startswith(upload_root + os.sep) or not os.path.isfile(doc_path):
            return {"submitted": False, "errors": ["Upload age proof through the official portal first; the supplied document reference was not found."]}
        data = {"applicant_name": applicant_name, "dob": date_of_birth, "village": village,
                "block": block, "bank_account": bank_account, "ifsc": ifsc,
                "gender": gender, "marital_status": marital_status, "husband_name": husband_name}
        errors, cleaned = portal.validate_application(data, doc_path)
        if errors:
            return {"submitted": False, "errors": errors}
        conn = db_connect()
        try:
            with conn.cursor() as cur:
                # Serialize concurrent submissions for this mobile to avoid duplicate active applications.
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (mobile,))
                active = portal.active_application(cur, mobile)
                if active:
                    conn.rollback()
                    return {"submitted": False, "errors": [f"This mobile already has an active application ({active[1]}, {active[2]})."], "existing_application_no": active[1]}
                app_id, app_no = portal.create_application(cur, mobile, cleaned)
                conn.commit()
            return {"submitted": True, "application_no": app_no,
                    "message": "Application submitted to the official portal and is pending review.",
                    "status": "PENDING", "portal_application_id": app_id}
        except Exception:
            conn.rollback()
            log.exception("citizen application submission failed")
            return {"submitted": False, "errors": ["Submission could not be completed. Please retry or use the official portal."]}
        finally:
            conn.close()

else:
    def _officer(ctx: Context):
        claims = get_claims(ctx)
        subject = claims["sub"]
        username = subject.split(":", 1)[1]
        return username

    @mcp.tool()
    def list_pending_applications(ctx: Context, block: str = "") -> dict:
        """List pending applications only in the deployment's configured officer block subset."""
        username = _officer(ctx)
        if block and block not in ALLOWED_BLOCKS:
            return {"applications": [], "error": "This block is outside this officer service's configured jurisdiction."}
        conn = db_connect()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if block:
                    cur.execute("SELECT id, application_no, applicant_name, village, block, submitted_at FROM applications WHERE status='PENDING' AND block=%s ORDER BY submitted_at ASC LIMIT 100", (block,))
                else:
                    cur.execute("SELECT id, application_no, applicant_name, village, block, submitted_at FROM applications WHERE status='PENDING' AND block = ANY(%s) ORDER BY submitted_at ASC LIMIT 100", (list(ALLOWED_BLOCKS),))
                rows = cur.fetchall()
            return {"officer": username, "allowed_blocks": list(ALLOWED_BLOCKS),
                    "applications": [{k: as_iso(v) for k, v in row.items()} for row in rows]}
        finally:
            conn.close()

    @mcp.tool()
    def inspect_application(ctx: Context, application_id: int) -> dict:
        """Inspect a case within the configured block subset; sensitive bank details are masked."""
        username = _officer(ctx)
        conn = db_connect()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id, application_no, applicant_name, mobile, dob, gender, marital_status, village, block, bank_account, ifsc, status, submitted_at, decided_at, decided_by FROM applications WHERE id=%s AND block=ANY(%s)", (application_id, list(ALLOWED_BLOCKS)))
                row = cur.fetchone()
                if not row:
                    return {"found": False, "message": "Application not found in this officer service's jurisdiction."}
                account = row.pop("bank_account", "") or ""
                row["bank_account_masked"] = ("*" * max(0, len(account)-4)) + account[-4:]
                cur.execute("SELECT at, action, actor, note FROM audit_log WHERE application_id=%s ORDER BY at ASC", (application_id,))
                audit = cur.fetchall()
            return {"found": True, "officer": username,
                    "application": {k: as_iso(v) for k, v in row.items()},
                    "audit": [{k: as_iso(v) for k, v in item.items()} for item in audit]}
        finally:
            conn.close()

    @mcp.tool()
    def decide_application(ctx: Context, application_id: int, decision: str, reason: str) -> dict:
        """Approve or reject a pending application with a mandatory reason recorded against the authenticated officer."""
        username = _officer(ctx)
        decision = (decision or "").strip().upper()
        reason = (reason or "").strip()
        if decision not in {"APPROVED", "REJECTED"}:
            return {"decided": False, "error": "Decision must be APPROVED or REJECTED."}
        if len(reason) < 8:
            return {"decided": False, "error": "Provide a meaningful reason of at least 8 characters."}
        if len(reason) > 500:
            return {"decided": False, "error": "Reason must be 500 characters or fewer."}
        conn = db_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE applications SET status=%s, decided_at=%s, decided_by=%s WHERE id=%s AND status='PENDING' AND block=ANY(%s) RETURNING application_no, block", (decision, portal.now_ist(), username, application_id, list(ALLOWED_BLOCKS)))
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return {"decided": False, "error": "Application is not pending or is outside this officer service's jurisdiction."}
                portal.write_audit(cur, application_id, decision, "officer:%s" % username, reason)
                conn.commit()
            return {"decided": True, "application_no": row[0], "status": decision,
                    "block": row[1], "decided_by": username, "reason_recorded": True}
        except Exception:
            conn.rollback()
            log.exception("officer decision failed")
            return {"decided": False, "error": "Decision could not be recorded; no success is being reported."}
        finally:
            conn.close()


# The MCP SDK exposes a Starlette ASGI app for streamable HTTP. Auth wraps the
# complete app so no tool can be invoked without the portal token.
http_app = mcp.streamable_http_app()
secured_app = BearerAuthASGI(http_app)

if __name__ == "__main__":
    uvicorn.run(secured_app, host="0.0.0.0", port=PORT, log_level="info")
