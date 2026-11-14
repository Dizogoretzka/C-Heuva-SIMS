import re


_CLASS_LABEL_PATTERN = re.compile(
    r'^\s*(?:grade\s+)?(?P<grade>1[0-2]|[1-9])'
    r'(?:\s*-?\s*(?P<letter>[a-g]))?\s*$',
    re.IGNORECASE,
)


class ClassLabelError(ValueError):
    pass


def parse_class_label(value):
    if value is None or not str(value).strip():
        return None

    match = _CLASS_LABEL_PATTERN.fullmatch(str(value))
    if not match:
        raise ClassLabelError(
            'Enter a grade from 1 to 12, optionally followed by a class '
            'letter from A to G (for example 8A, 8 a, or 9).')

    return int(match.group('grade')), (match.group('letter') or '').lower()


def format_class_label(grade_number, class_letter=''):
    if grade_number is None:
        return ''
    return f'Grade {grade_number}{(class_letter or "").upper()}'
