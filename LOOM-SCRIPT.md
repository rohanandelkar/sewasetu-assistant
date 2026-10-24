# Loom walkthrough outline (target: under 3 minutes)

**0:00–0:20 — The problem**
“Seva Setu already owns the pension workflow. The assistant is another way to use it, not a way to bypass its rules. The scheme requires applicants to be 60 or older, a valid age-proof document, and an open application window.”

**0:20–0:55 — Architecture**
“There's a citizen MCP endpoint and a separate officer MCP endpoint. Both verify portal-issued JWTs against the portal JWKS and check issuer, audience, role and scope. The citizen endpoint derives identity from the token; the officer endpoint only exposes tools for its configured block subset.”

**0:55–1:25 — Decisions**
“Citizen submission reuses the portal's validation and creation functions. Officer decisions require a reason, update only pending cases in jurisdiction, and write both the status decision and an audit record. The assistant never directly grants a pension.”

**1:25–2:15 — Personas and evaluation**
“Show Savitri Devi, age 70, applying with help from her grandson; Ram Prasad, age 58, who must not be allowed to apply; Kamla Devi, who must only see her own status; a Sonari officer; and a cross-role token probe. Show `/eval-dashboard` and explain which checks have actually run. Do not describe NOT_RUN scenarios as passed.”

**2:15–2:45 — What I learned / limitations**
“The main tradeoff is keeping portal rules authoritative while separating permissions by endpoint. The next improvement would be a portal-issued, short-lived age-proof reference and more end-to-end tests in a disposable database. The live MCP-client suite must be run against the deployment before claiming a pass rate.”
