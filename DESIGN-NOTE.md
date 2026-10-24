# Seva Setu Assistant — One-page Design Note

## Three decisions that mattered most

1. **The portal remains the system of record.** The citizen MCP calls the portal's existing `validate_application()` and `create_application()` functions instead of reimplementing scheme rules. Application status and officer decisions are read from and written to the existing Postgres tables. This reduces drift between the browser and assistant channels.

2. **Separate citizen and officer MCP processes.** The two roles run on different endpoints and expose different tool sets. Each request must carry a portal-issued RS256 token whose issuer, audience, role, scope, expiry, and subject shape are checked. Citizen status lookup derives the mobile number from the token rather than a tool argument. This makes accidental cross-role access harder and supports the requirement that citizen assistants never see officer tools.

3. **Constrain officer actions and make them auditable.** The officer service defaults to the `Sonari` block and can be widened only through an explicit `MCP_ALLOWED_BLOCKS` setting. Decisions are allowed only for pending cases in that subset, require a meaningful reason, update the application and append an audit entry in the same database transaction. The assistant never grants a pension outside the portal's existing decision flow.

## What I would change with more time

I would replace the current shared-volume age-proof handoff with a short-lived, portal-issued document reference that is bound to the authenticated citizen session. I would also add automated end-to-end tests against a disposable seeded database, including successful application submission, duplicate submission, withdrawal/reapply, decision races, and a complete real-client conversation transcript. The current evaluation runner is a smoke suite and deliberately does not submit or decide real production cases.
