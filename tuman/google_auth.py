"""Server-side Google OAuth code flow with PKCE and authenticated UserInfo.

Identity comes only from Google's fixed HTTPS UserInfo endpoint after a code
exchange authenticated with this application's client credentials. Browser-
supplied profiles and ID tokens are never accepted or decoded as identity.
"""
import base64
import hashlib
import json
import ssl
from urllib.parse import urlencode

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleOAuthError(ValueError):
    pass


def authorization_url(settings, state, verifier):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_callback_url,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    })


async def read_json(response):
    if response.status != 200:
        raise GoogleOAuthError("Google не подтвердил вход. Попробуйте ещё раз.")
    content = bytearray()
    async for chunk in response.content.iter_chunked(8192):
        content.extend(chunk)
        if len(content) > 64000:
            raise GoogleOAuthError("Некорректный ответ Google. Повторите вход.")
    try:
        value = json.loads(content)
    except (ValueError, UnicodeError):
        raise GoogleOAuthError("Некорректный ответ Google. Повторите вход.") from None
    if not isinstance(value, dict):
        raise GoogleOAuthError("Некорректный ответ Google. Повторите вход.")
    return value


def verified_profile(value):
    """Validate a response obtained by our server from authenticated UserInfo."""
    subject = value.get("sub")
    email = value.get("email")
    if (not isinstance(subject, str) or not 1 <= len(subject) <= 255
            or any(ord(char) < 33 or ord(char) > 126 for char in subject)
            or not isinstance(email, str) or not 3 <= len(email) <= 320
            or "@" not in email or any(char.isspace() for char in email)
            or value.get("email_verified") is not True):
        raise GoogleOAuthError("Google должен подтвердить адрес электронной почты.")
    name = value.get("name")
    return {"sub": subject, "email": email, "email_verified": True, "name": (name.strip() if isinstance(name, str) else "")[:100] or email.split("@", 1)[0][:100]}


async def exchange_code(settings, code, verifier):
    """No redirects, token persistence, user-controlled endpoints or TLS bypass."""
    try:
        connector = TCPConnector(ssl=ssl.create_default_context())
        async with ClientSession(connector=connector, timeout=ClientTimeout(total=15), trust_env=True) as session:
            async with session.post(TOKEN_URL, data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_callback_url,
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
            }, allow_redirects=False) as response:
                tokens = await read_json(response)
            access = tokens.get("access_token")
            if (not isinstance(access, str) or not 1 <= len(access) <= 8192
                    or any(ord(char) < 33 or ord(char) > 126 for char in access)
                    or str(tokens.get("token_type", "")).lower() != "bearer"):
                raise GoogleOAuthError("Google не подтвердил вход. Попробуйте ещё раз.")
            async with session.get(USERINFO_URL, headers={"Authorization": "Bearer " + access}, allow_redirects=False) as response:
                profile = await read_json(response)
        return verified_profile(profile)
    except (ClientError, TimeoutError, OSError):
        # Exception URLs/bodies may contain the code or a credential. Never emit them.
        raise GoogleOAuthError("Не удалось связаться с Google. Повторите вход позже.") from None
