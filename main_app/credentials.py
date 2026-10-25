import secrets
from functools import lru_cache

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone


_PIN_DIGITS = '0123456789'


def generate_pin():
    pin_length = settings.SIMS_PIN_LENGTH
    if not 4 <= pin_length <= 10:
        raise ValueError('SIMS_PIN_LENGTH must be between 4 and 10.')

    while True:
        pin = f'{secrets.randbelow(10 ** pin_length):0{pin_length}d}'
        try:
            return validate_pin(pin)
        except ValueError:
            continue


def validate_pin(pin):
    pin_length = settings.SIMS_PIN_LENGTH
    if not 4 <= pin_length <= 10:
        raise ValueError('SIMS_PIN_LENGTH must be between 4 and 10.')
    if (
        not isinstance(pin, str)
        or len(pin) != pin_length
        or not pin.isdigit()
        or len(set(pin)) == 1
        or pin in _PIN_DIGITS
        or pin in _PIN_DIGITS[::-1]
    ):
        raise ValueError(
            f'PIN must be a non-sequential {pin_length}-digit number.')
    return pin


def issue_pin(user, pin=None):
    pin = generate_pin() if pin is None else validate_pin(pin)
    update_fields = [
        'pin_hash',
        'pin_issued_at',
        'failed_login_attempts',
        'locked_until',
    ]
    if user.user_type in ('1', '3') and not user.is_superuser:
        user.set_unusable_password()
        update_fields.append('password')
    user.pin_hash = make_password(pin)
    user.pin_issued_at = timezone.now()
    user.failed_login_attempts = 0
    user.locked_until = None
    user.save(update_fields=update_fields)
    return pin


def verify_pin(pin, pin_hash):
    return check_password(
        pin or '',
        pin_hash or _dummy_pin_hash(tuple(settings.PASSWORD_HASHERS)),
    )


@lru_cache(maxsize=4)
def _dummy_pin_hash(password_hashers):
    return make_password('not-a-valid-pin')
