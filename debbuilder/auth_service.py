"""Authentication and OIDC helpers for the HTTP facade."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
import urllib.request
from collections.abc import Callable


def header_value(headers: dict, name: str) -> str:
    lowered = {str(k).lower(): str(v).strip() for k, v in headers.items()}
    return lowered.get(name.lower(), "")


def parse_cookies(header: str) -> dict[str, str]:
    out = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = urllib.parse.unquote(v.strip())
    return out


def sign_value(value: str, secret: str) -> str:
    sig = hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{sig}"


def unsign_value(value: str, secret: str) -> str | None:
    raw, sep, sig = (value or "").rpartition(".")
    if not sep:
        return None
    good = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return raw if hmac.compare_digest(sig, good) else None


def oidc_session_user(headers: dict, sessions: dict[str, dict], secret: str) -> str:
    cookies = parse_cookies(header_value(headers, "Cookie"))
    sid = unsign_value(cookies.get("debbuilder_session", ""), secret)
    if not sid:
        return ""
    row = sessions.get(sid)
    if not row or row.get("expires", 0) < time.time():
        sessions.pop(sid, None)
        return ""
    return row.get("user", "")


def is_request_authorized(
    headers: dict,
    *,
    auth_mode: str | None,
    effective_security: dict,
    auth_header: str,
    session_user: Callable[[dict], str],
) -> bool:
    mode = (auth_mode or effective_security["auth_mode"] or "none").lower()
    if mode in {"", "none", "off"}:
        return True
    if mode == "header":
        return bool(header_value(headers, auth_header))
    if mode == "oidc":
        return bool(session_user(headers))
    return False


def oidc_discovery(config: dict, *, urlopen=urllib.request.urlopen) -> dict:
    issuer = config["oidc_issuer"].rstrip("/")
    if not issuer:
        raise ValueError("OIDC issuer is not configured")
    with urlopen(f"{issuer}/.well-known/openid-configuration", timeout=15) as resp:
        document = json.loads(resp.read().decode())
    if document.get("issuer", "").rstrip("/") != issuer:
        raise ValueError("OIDC discovery issuer mismatch")
    for key in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint", "jwks_uri"):
        if not str(document.get(key, "")).startswith("https://"):
            raise ValueError(f"OIDC discovery has invalid {key}")
    return document


def oidc_authorize_url(
    return_to: str,
    *,
    config: dict,
    discovery: dict,
    sessions: dict[str, dict],
) -> tuple[str, str]:
    if not (config["oidc_client_id"] and config["oidc_redirect_uri"]):
        raise ValueError("OIDC client ID and redirect URI are required")
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
    safe_return = return_to if return_to.startswith("/") and not return_to.startswith("//") else "/"
    sessions[f"state:{state}"] = {
        "return_to": safe_return,
        "nonce": nonce,
        "code_verifier": code_verifier,
        "expires": time.time() + 600,
    }
    params = urllib.parse.urlencode({
        "client_id": config["oidc_client_id"],
        "redirect_uri": config["oidc_redirect_uri"],
        "response_type": "code",
        "scope": "openid profile email groups",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    return f"{discovery['authorization_endpoint']}?{params}", state


def b64json(value: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


def validate_rs256(
    jwt: str,
    jwks_uri: str,
    *,
    issuer: str,
    audience: str,
    nonce: str,
    urlopen=urllib.request.urlopen,
) -> dict:
    parts = jwt.split(".")
    if len(parts) != 3:
        raise ValueError("invalid OIDC ID token")
    header, claims = b64json(parts[0]), b64json(parts[1])
    if header.get("alg") != "RS256":
        raise ValueError("unsupported OIDC signing algorithm")
    with urlopen(jwks_uri, timeout=15) as resp:
        keys = json.loads(resp.read().decode()).get("keys", [])
    key = next((k for k in keys if k.get("kid") == header.get("kid") and k.get("kty") == "RSA"), None)
    if not key:
        raise ValueError("OIDC signing key not found")
    n = int.from_bytes(base64.urlsafe_b64decode(key["n"] + "=" * (-len(key["n"]) % 4)), "big")
    e = int.from_bytes(base64.urlsafe_b64decode(key["e"] + "=" * (-len(key["e"]) % 4)), "big")
    signature = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
    decoded = pow(int.from_bytes(signature, "big"), e, n).to_bytes((n.bit_length() + 7) // 8, "big")
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(f"{parts[0]}.{parts[1]}".encode()).digest()
    padding_len = len(decoded) - len(digest_info) - 3
    expected = b"\x00\x01" + b"\xff" * padding_len + b"\x00" + digest_info
    if padding_len < 8 or not hmac.compare_digest(decoded, expected):
        raise ValueError("invalid OIDC ID token signature")
    aud = claims.get("aud", [])
    if isinstance(aud, str):
        aud = [aud]
    now = time.time()
    if (
        claims.get("iss", "").rstrip("/") != issuer.rstrip("/")
        or audience not in aud
        or float(claims.get("exp", 0)) <= now
        or float(claims.get("iat", now + 1)) > now + 60
        or claims.get("nonce") != nonce
    ):
        raise ValueError("invalid OIDC ID token claims")
    return claims


def exchange_oidc_code(
    code: str,
    nonce: str,
    code_verifier: str,
    *,
    config: dict,
    discovery: dict,
    client_secret: str,
    validate_id_token: Callable[..., dict],
    urlopen=urllib.request.urlopen,
) -> dict:
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config["oidc_redirect_uri"],
        "client_id": config["oidc_client_id"],
        "client_secret": client_secret,
        "code_verifier": code_verifier,
    }).encode()
    req = urllib.request.Request(
        discovery["token_endpoint"],
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(req, timeout=20) as resp:
        token = json.loads(resp.read().decode())
    access = token.get("access_token")
    if not access:
        raise ValueError("OIDC token response has no access_token")
    claims = validate_id_token(
        str(token.get("id_token") or ""),
        discovery["jwks_uri"],
        issuer=config["oidc_issuer"],
        audience=config["oidc_client_id"],
        nonce=nonce,
    )
    req = urllib.request.Request(discovery["userinfo_endpoint"], headers={"Authorization": f"Bearer {access}"})
    with urlopen(req, timeout=20) as resp:
        userinfo = json.loads(resp.read().decode())
    if userinfo.get("sub") != claims.get("sub"):
        raise ValueError("OIDC userinfo subject mismatch")
    return userinfo


def create_session(userinfo: dict, sessions: dict[str, dict], signer: Callable[[str], str]) -> str:
    sid = secrets.token_urlsafe(32)
    user = userinfo.get("preferred_username") or userinfo.get("email") or userinfo.get("sub") or "user"
    sessions[sid] = {"user": user, "userinfo": userinfo, "expires": time.time() + 86400}
    return signer(sid)
