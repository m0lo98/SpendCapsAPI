import base64
import json

import pytest
from starlette.requests import Request

from app.audit import UNKNOWN_CALLER, caller_identity


def id_token(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.SIGNATURE_REMOVED_BY_GOOGLE"


def request_with(headers: dict) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw})


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Authorization": f"Bearer {id_token({'email': 'ops@example.org', 'sub': '1'})}"}, "ops@example.org"),
        ({"Authorization": f"Bearer {id_token({'sub': '1234'})}"}, "1234"),
        (
            {
                "X-Serverless-Authorization": f"Bearer {id_token({'email': 'iap@example.org'})}",
                "Authorization": f"Bearer {id_token({'email': 'custom@example.org'})}",
            },
            "iap@example.org",
        ),
        ({}, UNKNOWN_CALLER),
        ({"Authorization": "Basic abc"}, UNKNOWN_CALLER),
        ({"Authorization": "Bearer not-a-jwt"}, UNKNOWN_CALLER),
        ({"Authorization": "Bearer a.!!!.c"}, UNKNOWN_CALLER),
        ({"Authorization": f"Bearer a.{base64.urlsafe_b64encode(b'[1]').decode()}.c"}, UNKNOWN_CALLER),
    ],
    ids=["email", "sub-fallback", "serverless-header-first", "missing", "basic", "opaque", "bad-base64", "not-object"],
)
def test_caller_identity(headers, expected):
    assert caller_identity(request_with(headers)) == expected
