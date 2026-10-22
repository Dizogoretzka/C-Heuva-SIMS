import secrets
from functools import lru_cache

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone


_PIN_DIGITS = '0123456789'


def generate_pin():
    while True:
        pin = f'{secrets.randbelow(100000):05d}'
        if int(pin) < 1000 or len(set(pin)) == 1:
            continue
        if pin in _PIN_DIGITS or pin in _PIN_DIGITS[::-1]:
            continue
        return pin


def issue_pin(user):
    pin = generate_pin()
    user.pin_hash = make_password(pin)
    user.pin_issued_at = timezone.now()
    user.failed_login_attempts = 0
    user.locked_until = None
    user.save(update_fields=[
        'pin_hash',
        'pin_issued_at',
        'failed_login_attempts',
        'locked_until',
    ])
    return pin


def verify_pin(pin, pin_hash):
    return check_password(
        pin or '',
        pin_hash or _dummy_pin_hash(tuple(settings.PASSWORD_HASHERS)),
    )


@lru_cache(maxsize=4)
def _dummy_pin_hash(password_hashers):
    return make_password('not-a-valid-pin')
