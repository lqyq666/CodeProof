"""First-metered auth probe: verify the SDK can reuse the local Qoder sign-in.

Purpose tag: development. This is environment verification, not a product run.
Records session-credit start/end and the model request to the dev ledger.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from qodercn_agent_sdk import (
    QoderAgentOptions,
    QoderSDKClient,
    AssistantMessage,
    ResultMessage,
    access_token_from_env,
    qodercli_auth,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codeproof.ledger import Ledger  # noqa: E402

RUN = "dev-auth-probe"

# Stale host daemon markers make the spawned standalone CLI hang on initialize.
_DAEMON_VARS = (
    "QODER_AGENT_SDK_DAEMON",
    "QODER_AGENT_SDK_ENTRYPOINT",
    "QODERCLI_RUNTIME_PACKAGING",
    "QODER_SDK_AUTH_PAYLOAD_FILE",
    "QODER_WORKER_RUNTIME_ASSET_ROOT",
)


def select_auth():
    if os.environ.get("QODERCN_PERSONAL_ACCESS_TOKEN"):
        return "pat", access_token_from_env()
    return "qodercli", qodercli_auth()


async def main() -> int:
    led = Ledger(Path("evidence/m1/dev_ledger.jsonl"))
    auth_name, auth = select_auth()
    for var in _DAEMON_VARS:
        os.environ.pop(var, None)
    led.record(run_id=RUN, purpose="development", phase="env_prep", attempt=1,
               kind="probe_start", dedup_key=f"{RUN}:start:{auth_name}", source="sdk",
               payload={"auth": auth_name})
    options = QoderAgentOptions(auth=auth, allowed_tools=[],
                                max_turns=1, cwd=str(Path.cwd()),
                                env={"QODER_SKIP_VERSION_CHECK": "1"})
    client = QoderSDKClient(options=options)
    try:
        await client.connect()
        start = await client.get_usage_info()
        start_credits = None
        if start and getattr(start, "session", None):
            start_credits = start.session.total_credits
        led.record(run_id=RUN, purpose="development", phase="env_prep", attempt=1,
                   kind="session_credits", dedup_key=f"{RUN}:s:start:{start_credits}",
                   source="sdk", payload={"session_id": client.session_id,
                                          "role": "start",
                                          "total_credits": start_credits})
        print("session_id:", client.session_id, "| start_credits:", start_credits)

        await client.query("Reply with exactly: OK")
        result = None
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                u = msg.usage or {}
                led.record(run_id=RUN, purpose="development", phase="model_call",
                           attempt=1, kind="model_request",
                           dedup_key=f"{RUN}:req:{getattr(u,'request_id',msg.uuid)}",
                           source="sdk", payload={"model": msg.model, "usage": {
                               "input_tokens": getattr(u, "input_tokens", None),
                               "output_tokens": getattr(u, "output_tokens", None),
                               "cache_read_tokens": getattr(u, "cache_read_input_tokens", None),
                               "cache_write_tokens": getattr(u, "cache_creation_input_tokens", None),
                               "credits": getattr(u, "credits", None)}})
            elif isinstance(msg, ResultMessage):
                result = msg
                led.record(run_id=RUN, purpose="development", phase="model_call",
                           attempt=1, kind="session_credits",
                           dedup_key=f"{RUN}:s:end:{msg.total_credits}",
                           source="sdk", payload={"session_id": msg.session_id,
                                                  "role": "end",
                                                  "total_credits": msg.total_credits})
        end = await client.get_usage_info()
        end_credits = end.session.total_credits if end and getattr(end, "session", None) else None
        print("result subtype:", getattr(result, "subtype", None),
              "| is_error:", getattr(result, "is_error", None))
        print("result text:", getattr(result, "result", None))
        print("total_credits:", getattr(result, "total_credits", None),
              "| end_credits:", end_credits)
        print("usage:", getattr(result, "usage", None))
        ok = result is not None and not result.is_error
        led.record(run_id=RUN, purpose="development", phase="env_prep", attempt=1,
                   kind="probe_result", dedup_key=f"{RUN}:result", source="sdk",
                   payload={"auth_ok": ok, "subtype": getattr(result, "subtype", None)})
        return 0 if ok else 1
    except Exception as exc:
        led.record(run_id=RUN, purpose="development", phase="env_prep", attempt=1,
                   kind="probe_result", dedup_key=f"{RUN}:result", source="sdk",
                   payload={"auth_ok": False, "error": f"{exc.__class__.__name__}: {exc}"})
        print("PROBE FAILED:", exc.__class__.__name__, exc)
        return 2
    finally:
        await client.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))