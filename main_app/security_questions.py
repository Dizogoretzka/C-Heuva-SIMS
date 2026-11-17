import hashlib
import unicodedata


SECURITY_QUESTIONS = (
    "What was the name of your first school?",
    "What is your mother's maiden name?",
    "What is the name of the town where you were born?",
    "What was the name of your first teacher?",
    "What was the name of your first pet?",
    "What street did you grow up on?",
    "What was your childhood nickname?",
    "What is the name of your oldest childhood friend?",
)


def _remove_accents(value):
    decomposed = unicodedata.normalize('NFKD', str(value or ''))
    return ''.join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )


def normalize_security_answer(value):
    return ' '.join(_remove_accents(value).casefold().split())


def normalize_name_component(value):
    return ''.join(_remove_accents(value).casefold().split())


def placeholder_security_question(first_name, last_name, role):
    name = (
        normalize_name_component(first_name)
        + normalize_name_component(last_name)
        + str(role)
    )
    digest = hashlib.sha256(name.encode('utf-8')).digest()
    index = int.from_bytes(digest[:4], 'big') % len(SECURITY_QUESTIONS)
    return SECURITY_QUESTIONS[index]
