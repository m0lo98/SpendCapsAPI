import base64
import json

from fastapi import Request

UNKNOWN_CALLER = "unknown"


def caller_identity(request: Request) -> str:
    # Cloud Run IAM verifies the ID token and forwards it with the signature stripped, so only the claims are read.
    for header in ("x-serverless-authorization", "authorization"):
        scheme, _, token = request.headers.get(header, "").partition(" ")
        if scheme.lower() != "bearer":
            continue
        try:
            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            caller = claims.get("email") or claims.get("sub")
        except (IndexError, ValueError, AttributeError):
            continue
        if caller:
            return caller
    return UNKNOWN_CALLER


def log_audit(request: Request, action: str, billing_account_id: str, spend_cap_id: str, **details) -> None:
    entry = {
        "severity": "NOTICE",
        "message": f"spend cap {action}: {billing_account_id}/{spend_cap_id}",
        "audit": {
            "caller": caller_identity(request),
            "action": action,
            "billing_account_id": billing_account_id,
            "spend_cap_id": spend_cap_id,
            **details,
        },
    }
    print(json.dumps(entry, default=str), flush=True)
