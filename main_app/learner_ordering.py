import unicodedata


def _normalized_name(value):
    return ''.join(
        character
        for character in unicodedata.normalize(
            'NFKD', (value or '').casefold())
        if not unicodedata.combining(character)
    )


def learner_name_sort_key(last_name, first_name, account_id=''):
    return (
        _normalized_name(last_name),
        _normalized_name(first_name),
        _normalized_name(account_id),
    )


def learner_sort_key(account):
    return learner_name_sort_key(
        account.last_name,
        account.first_name,
        account.username,
    )


def sort_learners(learners, account_getter):
    return sorted(
        learners,
        key=lambda learner: learner_sort_key(account_getter(learner)),
    )
