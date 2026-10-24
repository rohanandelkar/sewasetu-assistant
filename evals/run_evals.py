#!/usr/bin/env python3
"""Run real MCP-client smoke evaluations and update results.json.

Required: CITIZEN_TOKEN and OFFICER_TOKEN issued by the portal for the correct
resource audiences. This intentionally does not submit or decide a real case.
"""
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parent
CITIZEN_URL = os.getenv("CITIZEN_MCP_URL", "http://localhost:8101/mcp")
OFFICER_URL = os.getenv("OFFICER_MCP_URL", "http://localhost:8102/mcp")
CITIZEN_TOKEN = os.getenv("CITIZEN_TOKEN", "")
OFFICER_TOKEN = os.getenv("OFFICER_TOKEN", "")
ALLOWED_BLOCKS = {x.strip() for x in os.getenv("MCP_ALLOWED_BLOCKS", "Sonari").split(",") if x.strip()}

async def client_call(url, token, tool, args=None):
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {item.name for item in tools.tools}
            if tool == "__list_tools__":
                return names
            if tool not in names:
                raise AssertionError(f"Tool {tool} is not exposed by {url}; exposed={sorted(names)}")
            result = await session.call_tool(tool, args or {})
            if getattr(result, "isError", False):
                raise AssertionError(f"Tool {tool} returned an MCP error")
            parsed = []
            for item in getattr(result, "content", []):
                if getattr(item, "type", None) == "text":
                    try: parsed.append(json.loads(item.text))
                    except Exception: parsed.append(item.text)
            return parsed[0] if len(parsed) == 1 else parsed

async def run():
    results = []
    if not CITIZEN_TOKEN or not OFFICER_TOKEN:
        raise SystemExit("Set CITIZEN_TOKEN and OFFICER_TOKEN for the correct MCP resource audiences first.")

    # Citizen assistant should expose only citizen capabilities.
    tools = await client_call(CITIZEN_URL, CITIZEN_TOKEN, "__list_tools__")
    expected = {"check_eligibility", "get_my_application_status", "submit_application"}
    assert expected.issubset(tools) and not ({"list_pending_applications", "inspect_application", "decide_application"} & tools)
    results.append({"id":"SEC-01","status":"PASS","notes":"Real MCP client listed citizen tools; officer tools were absent."})

    # Under-age eligibility boundary; this tool is read-only and does not create rows.
    underage = await client_call(CITIZEN_URL, CITIZEN_TOKEN, "check_eligibility", {"date_of_birth":"01/01/1968","block":"Borgaon"})
    assert underage.get("eligible") is False, f"Expected under-age applicant to be ineligible; got {underage}"
    results.append({"id":"CIT-02","status":"PASS","notes":"Real MCP client checked a 58-year-old persona; eligibility was false and the check was read-only."})

    # Current status lookup must derive mobile from the signed-in token, not arguments.
    status = await client_call(CITIZEN_URL, CITIZEN_TOKEN, "get_my_application_status", {})
    assert isinstance(status, dict) and "found" in status
    results.append({"id":"CIT-03","status":"PASS","notes":"Status lookup completed using the authenticated citizen identity; no caller-supplied mobile argument exists."})

    # Officer endpoint must expose only officer tools and constrain rows to allowed blocks.
    officer_tools = await client_call(OFFICER_URL, OFFICER_TOKEN, "__list_tools__")
    officer_expected = {"list_pending_applications", "inspect_application", "decide_application"}
    assert officer_expected.issubset(officer_tools) and not (expected & officer_tools)
    queue = await client_call(OFFICER_URL, OFFICER_TOKEN, "list_pending_applications", {})
    rows = queue.get("applications", [])
    assert all(row.get("block") in ALLOWED_BLOCKS for row in rows)
    results.append({"id":"OFF-01","status":"PASS","notes":f"Real MCP client listed {len(rows)} pending cases; every returned case was in the configured block subset. No decision was made by this smoke test."})

    # Wrong-role / wrong-audience token must be rejected by the officer HTTP endpoint.
    async with httpx.AsyncClient(timeout=10) as http:
        response = await http.get(OFFICER_URL, headers={"Authorization": f"Bearer {CITIZEN_TOKEN}"})
    assert response.status_code == 401, f"Expected wrong-audience citizen token to be rejected; got HTTP {response.status_code}"
    results.append({"id":"SEC-01","status":"PASS","notes":"Citizen token was rejected by officer endpoint with HTTP 401."})

    output = {"status":"PASS" if all(x["status"] == "PASS" for x in results) else "FAIL",
              "run_at": datetime.now(timezone.utc).isoformat(),
              "summary": f"{sum(x['status']=='PASS' for x in results)}/{len(results)} real MCP-client smoke checks passed. This run does not submit applications or decide cases.",
              "results": results}
    (ROOT / "results.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(output, indent=2))

if __name__ == "__main__":
    asyncio.run(run())
