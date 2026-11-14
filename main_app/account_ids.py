import unicodedata


ACCOUNT_ID_MAX_LENGTH = 160


def _account_id_part(value):
    normalized = unicodedata.normalize('NFKD', value or '')
    parts = []
    for character in normalized:
        if unicodedata.category(character).startswith('M'):
            continue
        parts.extend(
            folded for folded in character.casefold() if folded.isalnum())
    return ''.join(parts)


def generate_account_id(first_name, last_name, reserved=()):
    surname = _account_id_part(last_name)
    first_initial = _account_id_part(first_name)[:1]
    if not surname or not first_initial:
        raise ValueError(
            'First and last names are required to generate an account ID.')

    reserved_ids = {value.casefold() for value in reserved if value}
    suffix_number = 1
    while True:
        suffix = '' if suffix_number == 1 else str(suffix_number)
        surname_length = ACCOUNT_ID_MAX_LENGTH - len(first_initial) - len(suffix)
        candidate = (
            f'{surname[:surname_length]}{first_initial}{suffix}')
        if candidate.casefold() not in reserved_ids:
            return candidate
        suffix_number += 1
