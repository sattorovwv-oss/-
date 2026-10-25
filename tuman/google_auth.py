"""Server-side Google OAuth code flow with PKCE and authenticated UserInfo.

Identity comes only from Google's fixed HTTPS UserInfo endpoint after a code
exchange authenticated with this application's client credentials. Browser-
supplied profiles and ID tokens are never accepted or decoded as identity.
"""
import base64
import hashlib
import json
import logging
import ssl
from urllib.parse import urlencode

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
logger = logging.getLogger(__name__)

PROVIDER_ERRORS = {
    "invalid_client": "Google не принял настройки входа (invalid_client). Сообщите владельцу сайта.",
    "invalid_grant": "Google отклонил одноразовый код входа (invalid_grant). Начните вход заново.",
    "redirect_uri_mismatch": "Адрес возврата не совпадает с настройками Google (redirect_uri_mismatch). Сообщите владельцу сайта.",
    "unauthorized_client": "Для этого Google-приложения вход недоступен (unauthorized_client). Сообщите владельцу сайта.",
    "invalid_request": "Google отклонил запрос входа (invalid_request). Начните вход заново.",
    "invalid_scope": "Google не разрешил запрошенные данные профиля (invalid_scope). Сообщите владельцу сайта.",
    "invalid_token": "Google отклонил доступ к профилю (invalid_token). Начните вход заново.",
    "insufficient_scope": "Google не предоставил доступ к профилю (insufficient_scope). Сообщите владельцу сайта.",
    "access_denied": "Google не разрешил вход (access_denied). Проверьте доступ к приложению.",
    "temporarily_unavailable": "Google временно недоступен (temporarily_unavailable). Повторите вход позже.",
    "server_error": "Google временно недоступен (server_error). Повторите вход позже.",
}


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


async def read_json(response, stage="token"):
    content = bytearray()
    async for chunk in response.content.iter_chunked(8192):
        content.extend(chunk)
        if len(content) > 64000:
            raise GoogleOAuthError("Некорректный ответ Google. Повторите вход.")
    try:
        value = json.loads(content)
    except (ValueError, UnicodeError):
        value = None
    if response.status != 200:
        # Never echo Google's response, description, request URL or token values.
        error = value.get("error") if isinstance(value, dict) else None
        code = error if isinstance(error, str) and error in PROVIDER_ERRORS else "unknown"
        logger.warning("Google OAuth rejected request: stage=%s status=%s error=%s", stage, response.status, code)
        if code in PROVIDER_ERRORS:
            raise GoogleOAuthError(PROVIDER_ERRORS[code])
        if response.status == 429 or response.status >= 500:
            raise GoogleOAuthError(f"Google временно недоступен (HTTP {response.status}). Повторите вход позже.")
        label = "обмен кода входа" if stage == "token" else "получение профиля"
        raise GoogleOAuthError(f"Google отклонил {label} (HTTP {response.status}). Сообщите владельцу сайта.")
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
                tokens = await read_json(response, "token")
            access = tokens.get("access_token")
            if (not isinstance(access, str) or not 1 <= len(access) <= 8192
                    or any(ord(char) < 33 or ord(char) > 126 for char in access)
                    or str(tokens.get("token_type", "")).lower() != "bearer"):
                logger.warning("Google OAuth rejected response: stage=token error=invalid_token_response")
                raise GoogleOAuthError("Google вернул неполные данные входа (invalid_token_response). Сообщите владельцу сайта.")
            async with session.get(USERINFO_URL, headers={"Authorization": "Bearer " + access}, allow_redirects=False) as response:
                profile = await read_json(response, "userinfo")
        return verified_profile(profile)
    except (ClientError, TimeoutError, OSError):
        # Exception URLs/bodies may contain the code or a credential. Never emit them.
        raise GoogleOAuthError("Не удалось связаться с Google. Повторите вход позже.") from None
