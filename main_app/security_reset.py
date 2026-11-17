import hashlib
import secrets
from datetime import timedelta
from functools import lru_cache

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.db import transaction
from django.http import HttpResponseNotFound
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.module_loading import import_string
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .credentials import issue_pin
from .forms import (
    SecurityQuestionUpdateForm,
    SecurityResetAnswerForm,
    SecurityResetLookupForm,
)
from .models import ActivityLog, CustomUser
from .security_questions import (
    normalize_name_component,
    placeholder_security_question,
)


RESET_SESSION_KEY = 'security_question_reset'
RESET_TOKEN_LIFETIME_SECONDS = 600
RESET_FAILURE_LIMIT = 5
RESET_LOCKOUT_SECONDS = 60 * 60
RESET_IP_CACHE_PREFIX = 'sims-security-reset-ip:'
RESET_ROLE_TYPES = {
    'admin': '1',
    'teacher': '3',
    'learner': '2',
}
RESET_ROLE_LABELS = {
    'admin': 'Administrator',
    'teacher': 'Teacher',
    'learner': 'Learner',
}


@lru_cache(maxsize=4)
def _dummy_answer_hash(password_hashers):
    hasher_class = import_string(password_hashers[0])
    return make_password(
        'SIMS security question dummy answer',
        hasher=hasher_class.algorithm,
    )


def security_question_profile_form(request, user):
    if (
        request.method == 'POST'
        and request.POST.get('action') == 'security_question'
    ):
        form = SecurityQuestionUpdateForm(request.POST, user=user)
        if form.is_valid():
            old_question = user.security_question
            new_question = form.cleaned_data['security_question']
            with transaction.atomic():
                user.security_question = new_question
                user.security_answer_hash = make_password(
                    form.cleaned_data['answer'])
                user.save(update_fields=[
                    'security_question',
                    'security_answer_hash',
                ])
                role_label = {
                    '1': 'Administrator',
                    '2': 'Learner',
                    '3': 'Teacher',
                }[user.user_type]
                change = 'changed' if old_question else 'set'
                ActivityLog.objects.create(
                    actor=user,
                    actor_username=user.get_username() or '',
                    target_user=user,
                    target_username=user.get_username() or '',
                    target_user_id_snapshot=str(user.pk),
                    action='security_reset',
                    object_type='security_question',
                    object_id=str(user.pk),
                    description=(
                        f'{role_label} {user.get_full_name()} {change} '
                        'their security question.'
                    )[:255],
                    changes={},
                )
            messages.success(request, 'Security question saved.')
            return form, True
        return form, False
    return SecurityQuestionUpdateForm(user=user), False


def _activity_name(first_name, last_name):
    return ' '.join(
        f'{first_name or ""} {last_name or ""}'.split()
    )[:150]


def _record_reset_activity(
    description, typed_name, role, user=None, completed=False
):
    role_label = RESET_ROLE_LABELS[role]
    ActivityLog.objects.create(
        actor=user if completed else None,
        actor_username=(
            user.get_username() or ''
            if completed and user else (typed_name or '')[:150]
        ),
        target_user=user,
        target_username=user.get_username() or '' if user else '',
        target_user_id_snapshot=str(user.pk) if user else '',
        action='security_reset',
        object_type=(
            'self_service_reset_completed'
            if completed else 'self_service_reset'
        ),
        object_id=str(user.pk) if user else '',
        description=(
            f'{role_label} self-service reset {description} for '
            f'"{typed_name}".'
        )[:255],
        changes={},
    )


def _reset_ip_cache_key(request):
    address = request.META.get('REMOTE_ADDR', '')
    digest = hashlib.sha256(address.encode('utf-8')).hexdigest()
    return f'{RESET_IP_CACHE_PREFIX}{digest}'


def _reset_ip_limited(request):
    return cache.get(_reset_ip_cache_key(request), 0) >= (
        settings.SIMS_LOGIN_IP_MAX_FAILURES)


def _record_reset_ip_failure(request):
    key = _reset_ip_cache_key(request)
    if cache.add(
        key,
        1,
        timeout=settings.SIMS_LOGIN_IP_WINDOW_SECONDS,
    ):
        return
    try:
        cache.incr(key)
    except ValueError:
        cache.add(
            key,
            1,
            timeout=settings.SIMS_LOGIN_IP_WINDOW_SECONDS,
        )


def _matching_accounts(role, cleaned_data):
    user_type = RESET_ROLE_TYPES[role]
    accounts = CustomUser.objects.filter(
        user_type=user_type,
        is_superuser=False,
    ).select_related('student__school_class')
    if role == 'learner':
        grade_number, class_letter = cleaned_data['grade_and_class']
        accounts = accounts.filter(
            student__school_class__grade_number=grade_number,
            student__school_class__class_letter__iexact=class_letter,
        )

    first_name = normalize_name_component(cleaned_data['first_name'])
    last_name = normalize_name_component(cleaned_data['last_name'])
    name_matches = [
        account for account in accounts
        if (
            normalize_name_component(account.first_name) == first_name
            and normalize_name_component(account.last_name) == last_name
        )
    ]
    account_id = (cleaned_data.get('account_id') or '').strip()
    if account_id:
        name_matches = [
            account for account in name_matches
            if (account.username or '').casefold() == account_id.casefold()
        ]
    return name_matches


def _begin_question_step(
    request,
    role,
    first_name,
    last_name,
    user=None,
    ip_blocked=False,
):
    typed_name = _activity_name(first_name, last_name)
    now = timezone.now()
    has_real_question = bool(
        user
        and user.security_question
        and user.security_answer_hash
        and not (
            user.security_reset_locked_until
            and user.security_reset_locked_until > now
        )
        and not ip_blocked
    )
    question = (
        user.security_question if has_real_question
        else placeholder_security_question(first_name, last_name, role)
    )
    request.session[RESET_SESSION_KEY] = {
        'stage': 'answer',
        'role': role,
        'user_id': user.pk if user else None,
        'typed_name': typed_name,
        'question': question,
        'has_real_question': has_real_question,
        'ip_blocked': ip_blocked,
    }
    request.session.set_expiry(RESET_TOKEN_LIFETIME_SECONDS)
    return render(request, 'main_app/security_reset.html', {
        'stage': 'answer',
        'role': role,
        'role_label': RESET_ROLE_LABELS[role],
        'typed_name': typed_name,
        'question': question,
    })


def _resolve_disambiguation(request, role, state, form):
    candidate_ids = state.get('candidate_ids', [])
    account_id = form.cleaned_data.get('account_id', '').strip()
    candidates = list(CustomUser.objects.filter(
        pk__in=candidate_ids,
        user_type=RESET_ROLE_TYPES[role],
        is_superuser=False,
        username__iexact=account_id,
    ))
    if len(candidates) != 1:
        request.session.pop(RESET_SESSION_KEY, None)
        return render(request, 'main_app/security_reset.html', {
            'stage': 'lookup',
            'role': role,
            'role_label': RESET_ROLE_LABELS[role],
            'lookup_form': SecurityResetLookupForm(
                role=role,
                account_id_only=True,
            ),
            'generic_error': (
                'We cannot reset this account online. Ask your teacher or '
                'administrator.'
            ),
        })
    return _begin_question_step(
        request,
        role,
        state['first_name'],
        state['last_name'],
        user=candidates[0],
        ip_blocked=_reset_ip_limited(request),
    )


def _lookup_step(request, role):
    state = request.session.get(RESET_SESSION_KEY, {})
    if state.get('stage') == 'lookup_disambiguation':
        form = SecurityResetLookupForm(
            request.POST,
            role=role,
            account_id_required=True,
            account_id_only=True,
        )
        if not form.is_valid():
            return render(request, 'main_app/security_reset.html', {
                'stage': 'lookup',
                'role': role,
                'role_label': RESET_ROLE_LABELS[role],
                'lookup_form': form,
                'account_id_required': True,
            })
        return _resolve_disambiguation(request, role, state, form)

    form = SecurityResetLookupForm(request.POST, role=role)
    if not form.is_valid():
        return render(request, 'main_app/security_reset.html', {
            'stage': 'lookup',
            'role': role,
            'role_label': RESET_ROLE_LABELS[role],
            'lookup_form': form,
        })

    cleaned_data = form.cleaned_data
    first_name = cleaned_data['first_name'].strip()
    last_name = cleaned_data['last_name'].strip()
    typed_name = _activity_name(first_name, last_name)
    candidates = []
    if not _reset_ip_limited(request):
        candidates = _matching_accounts(role, cleaned_data)

    _record_reset_activity(
        'started',
        typed_name,
        role,
        user=candidates[0] if len(candidates) == 1 else None,
    )

    if _reset_ip_limited(request):
        _record_reset_activity(
            'was blocked by the per-IP attempt limit',
            typed_name,
            role,
        )
        return _begin_question_step(
            request,
            role,
            first_name,
            last_name,
            ip_blocked=True,
        )

    if role == 'learner' and len(candidates) > 1:
        request.session[RESET_SESSION_KEY] = {
            'stage': 'lookup_disambiguation',
            'role': role,
            'candidate_ids': [account.pk for account in candidates],
            'first_name': first_name,
            'last_name': last_name,
        }
        request.session.set_expiry(RESET_TOKEN_LIFETIME_SECONDS)
        return render(request, 'main_app/security_reset.html', {
            'stage': 'lookup',
            'role': role,
            'role_label': RESET_ROLE_LABELS[role],
            'lookup_form': SecurityResetLookupForm(
                role=role,
                account_id_required=True,
                account_id_only=True,
            ),
            'account_id_required': True,
        })

    return _begin_question_step(
        request,
        role,
        first_name,
        last_name,
        user=candidates[0] if len(candidates) == 1 else None,
    )


def _record_account_failure(user_id):
    now = timezone.now()
    with transaction.atomic():
        account = CustomUser.objects.select_for_update().filter(
            pk=user_id,
            user_type__in=RESET_ROLE_TYPES.values(),
            is_superuser=False,
        ).first()
        if account is None:
            return False, None
        if (
            account.security_reset_locked_until
            and account.security_reset_locked_until > now
        ):
            return False, account
        if account.security_reset_locked_until:
            account.security_reset_failed_attempts = 0
            account.security_reset_locked_until = None
        account.security_reset_failed_attempts += 1
        newly_locked = (
            account.security_reset_failed_attempts >= RESET_FAILURE_LIMIT)
        if newly_locked:
            account.security_reset_locked_until = (
                now + timedelta(seconds=RESET_LOCKOUT_SECONDS))
        account.save(update_fields=[
            'security_reset_failed_attempts',
            'security_reset_locked_until',
        ])
        return newly_locked, account


def _answer_step(request, role, state):
    typed_name = state.get('typed_name', '')
    user = CustomUser.objects.filter(
        pk=state.get('user_id'),
        user_type=RESET_ROLE_TYPES[role],
        is_superuser=False,
    ).first() if state.get('user_id') else None
    has_real_question = bool(
        user
        and state.get('has_real_question')
        and user.security_question == state.get('question')
        and user.security_answer_hash
    )
    answer_hash = (
        user.security_answer_hash if has_real_question
        else _dummy_answer_hash(tuple(settings.PASSWORD_HASHERS))
    )
    answer_form = SecurityResetAnswerForm(request.POST)
    normalized_answer = (
        answer_form.cleaned_data['answer']
        if answer_form.is_valid()
        else ''
    )
    answer_matches = check_password(normalized_answer, answer_hash)
    ip_blocked = (
        state.get('ip_blocked') or _reset_ip_limited(request))
    account_locked = bool(
        user
        and user.security_reset_locked_until
        and user.security_reset_locked_until > timezone.now()
    )

    if ip_blocked:
        _record_reset_activity(
            'was blocked by the per-IP attempt limit',
            typed_name,
            role,
            user=user,
        )
    elif account_locked:
        _record_reset_activity(
            'was blocked because the account is temporarily locked',
            typed_name,
            role,
            user=user,
        )
    elif has_real_question and answer_matches and user:
        with transaction.atomic():
            locked_user = CustomUser.objects.select_for_update().filter(
                pk=user.pk,
                user_type=RESET_ROLE_TYPES[role],
                is_superuser=False,
            ).first()
            if (
                locked_user
                and locked_user.security_question == state.get('question')
                and locked_user.security_answer_hash
                and check_password(
                    normalized_answer,
                    locked_user.security_answer_hash,
                )
                and not (
                    locked_user.security_reset_locked_until
                    and locked_user.security_reset_locked_until
                    > timezone.now()
                )
            ):
                locked_user.security_reset_failed_attempts = 0
                locked_user.security_reset_locked_until = None
                locked_user.save(update_fields=[
                    'security_reset_failed_attempts',
                    'security_reset_locked_until',
                ])
                token = secrets.token_urlsafe(32)
                state.update({
                    'stage': 'new_secret',
                    'token': token,
                    'expires_at': (
                        timezone.now().timestamp()
                        + RESET_TOKEN_LIFETIME_SECONDS
                    ),
                })
                request.session[RESET_SESSION_KEY] = state
                request.session.set_expiry(RESET_TOKEN_LIFETIME_SECONDS)
                return render(request, 'main_app/security_reset.html', {
                    'stage': 'new_secret',
                    'role': role,
                    'role_label': RESET_ROLE_LABELS[role],
                    'reset_token': token,
                    'password_form': SetPasswordForm(user),
                })
        _record_reset_activity(
            'was blocked because the account is temporarily locked',
            typed_name,
            role,
            user=user,
        )
    else:
        newly_locked, current_user = (
            _record_account_failure(user.pk)
            if user else (False, None)
        )
        _record_reset_ip_failure(request)
        _record_reset_activity(
            'answer did not match',
            typed_name,
            role,
            user=current_user or user,
        )
        if newly_locked:
            _record_reset_activity(
                'was blocked for one hour after repeated wrong answers',
                typed_name,
                role,
                user=current_user or user,
            )

    return render(request, 'main_app/security_reset.html', {
        'stage': 'answer',
        'role': role,
        'role_label': RESET_ROLE_LABELS[role],
        'typed_name': typed_name,
        'question': state.get('question'),
        'generic_error': 'The details did not match.',
    })


def _revoke_user_sessions(user):
    user_id = str(user.pk)
    active_sessions = Session.objects.filter(
        expire_date__gt=timezone.now())
    for session in active_sessions.iterator():
        if session.get_decoded().get('_auth_user_id') == user_id:
            session.delete()


def _complete_reset(request, role, state):
    user = CustomUser.objects.filter(
        pk=state.get('user_id'),
        user_type=RESET_ROLE_TYPES[role],
        is_superuser=False,
    ).first()
    submitted_token = request.POST.get('reset_token', '')
    if (
        state.get('stage') != 'new_secret'
        or state.get('expires_at', 0) <= timezone.now().timestamp()
        or not state.get('token')
        or not constant_time_compare(
            submitted_token, state.get('token', ''))
        or user is None
    ):
        request.session.pop(RESET_SESSION_KEY, None)
        messages.error(request, 'Please start the reset process again.')
        return redirect(reverse('security_reset', args=[role]))

    new_pin = None
    now = timezone.now()
    if role == 'learner':
        password_form = SetPasswordForm(user, request.POST)
        if not password_form.is_valid():
            return render(request, 'main_app/security_reset.html', {
                'stage': 'new_secret',
                'role': role,
                'role_label': RESET_ROLE_LABELS[role],
                'reset_token': state['token'],
                'password_form': password_form,
            })

    with transaction.atomic():
        user = CustomUser.objects.select_for_update().get(pk=user.pk)
        if role == 'learner':
            user.set_password(password_form.cleaned_data['new_password1'])
        else:
            new_pin = issue_pin(user)

        user.failed_login_attempts = 0
        user.locked_until = None
        user.password_setup_required = False
        user.security_reset_failed_attempts = 0
        user.security_reset_locked_until = None
        user.security_reset_at = now
        user.security_reset_notice_pending = True
        user.save(update_fields=[
            'password',
            'failed_login_attempts',
            'locked_until',
            'password_setup_required',
            'security_reset_failed_attempts',
            'security_reset_locked_until',
            'security_reset_at',
            'security_reset_notice_pending',
        ])
        _revoke_user_sessions(user)
        _record_reset_activity(
            'completed',
            state.get('typed_name', ''),
            role,
            user=user,
            completed=True,
        )

    request.session.pop(RESET_SESSION_KEY, None)
    request.session.set_expiry(None)
    request.session.cycle_key()
    response = render(request, 'main_app/landing.html', {
        'selected_role': role,
        'pin_length': settings.SIMS_PIN_LENGTH,
        'today': timezone.localdate(),
        'reset_pin_once': new_pin,
        'reset_success_message': (
            'Your password or PIN was reset. Sign in with your new secret.'
        ),
    })
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
    response['Pragma'] = 'no-cache'
    return response


@require_http_methods(['GET', 'POST'])
@never_cache
def security_reset(request, role):
    if role not in RESET_ROLE_TYPES:
        return HttpResponseNotFound()
    if request.user.is_authenticated:
        if request.user.is_superuser:
            return HttpResponseNotFound()
        profile_route = {
            '1': 'admin_view_profile',
            '3': 'teacher_profile',
            '2': 'student_view_profile',
        }.get(request.user.user_type)
        if not profile_route:
            return HttpResponseNotFound()
        return redirect(reverse(profile_route))

    if request.method == 'GET':
        state = request.session.get(RESET_SESSION_KEY, {})
        if (
            state.get('stage') == 'new_secret'
            and state.get('role') == role
            and state.get('expires_at', 0) > timezone.now().timestamp()
        ):
            user = CustomUser.objects.filter(
                pk=state.get('user_id'),
                user_type=RESET_ROLE_TYPES[role],
                is_superuser=False,
            ).first()
            if user:
                return render(request, 'main_app/security_reset.html', {
                    'stage': 'new_secret',
                    'role': role,
                    'role_label': RESET_ROLE_LABELS[role],
                    'reset_token': state['token'],
                    'password_form': SetPasswordForm(user),
                })
        if state.get('stage') == 'answer' and state.get('role') == role:
            return render(request, 'main_app/security_reset.html', {
                'stage': 'answer',
                'role': role,
                'role_label': RESET_ROLE_LABELS[role],
                'typed_name': state.get('typed_name', ''),
                'question': state.get('question'),
            })
        request.session.pop(RESET_SESSION_KEY, None)
        return render(request, 'main_app/security_reset.html', {
            'stage': 'lookup',
            'role': role,
            'role_label': RESET_ROLE_LABELS[role],
            'lookup_form': SecurityResetLookupForm(role=role),
        })

    action = request.POST.get('action')
    if action == 'lookup':
        return _lookup_step(request, role)

    state = request.session.get(RESET_SESSION_KEY, {})
    if state.get('role') != role:
        request.session.pop(RESET_SESSION_KEY, None)
        return redirect(reverse('security_reset', args=[role]))
    if action == 'answer' and state.get('stage') == 'answer':
        return _answer_step(request, role, state)
    if action == 'reset' and state.get('stage') == 'new_secret':
        return _complete_reset(request, role, state)
    return redirect(reverse('security_reset', args=[role]))
