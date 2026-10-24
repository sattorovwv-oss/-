import base64
import hashlib
import hmac
import re
import secrets
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def password_hash(password):
    if not 12 <= len(password) <= 128:
        raise ValueError("Пароль: от 12 до 128 символов / Рамз: аз 12 то 128 аломат")
    salt = secrets.token_bytes(16)
    value = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(value).decode()


def password_matches(password, encoded):
    if len(password) > 128:
        return False
    try:
        _, salt, expected = encoded.split("$")
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(actual, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def username(value):
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{3,40}", value):
        raise ValueError("Логин: 3–40 латинских букв, цифр или _")
    return value


def track_code(value):
    value = "".join(value.split()).upper()
    if not re.fullmatch(r"[A-Z0-9-]{6,40}", value):
        raise ValueError("Трек-код: 6–40 латинских букв, цифр или дефисов")
    return value


def number(value, maximum=1_000_000, zero=False):
    try:
        n = Decimal("".join(str(value).split()).replace(",", "."))
    except InvalidOperation:
        raise ValueError("Введите число / Рақам ворид кунед") from None
    if not n.is_finite() or n < 0 or (n == 0 and not zero) or n > maximum:
        raise ValueError("Число вне допустимого диапазона / Рақам нодуруст аст")
    return n


def minor(value):
    return int((number(value, zero=True) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def display(value):
    return format(Decimal(str(value)).normalize(), "f")
