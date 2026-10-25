from contextvars import ContextVar
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from .models import (
    ActivityLog,
    Attendance,
    AttendanceReport,
    Course,
    CustomUser,
    FeedbackStudent,
    GradeBand,
    LeaveReportStudent,
    NotificationStudent,
    Session,
    Student,
    StudentResult,
    Subject,
    Teacher,
)


_current_request = ContextVar('sims_activity_request', default=None)
_SENSITIVE_FIELDS = {
    'password',
    'pin_hash',
    'fcm_token',
    'address',
    'profile_pic',
}
_TRACKED_MODELS = (
    CustomUser,
    Teacher,
    Student,
    Subject,
    StudentResult,
    Attendance,
    AttendanceReport,
    Course,
    Session,
    GradeBand,
    LeaveReportStudent,
    FeedbackStudent,
    NotificationStudent,
)


def set_current_request(request):
    return _current_request.set(request)


def reset_current_request(token):
    _current_request.reset(token)


def _request_details():
    request = _current_request.get()
    if request is None:
        return None, None
    user = getattr(request, 'user', None)
    actor = user if user and user.is_authenticated else None
    return actor, request.META.get('REMOTE_ADDR')


def _json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime, Decimal)):
        return str(value)
    return str(value)


def _field_value(instance, field):
    value = getattr(instance, field.attname)
    if field.name in _SENSITIVE_FIELDS:
        return '[redacted]'
    if field.is_relation:
        if value is None:
            return None
        if field.name == 'teacher' and field.related_model is Teacher:
            related = Teacher.objects.select_related('teacher').filter(
                pk=value).first()
            return str(related) if related else str(value)
        return str(value)
    return _json_value(value)


def _target_user(instance):
    if isinstance(instance, CustomUser):
        return instance
    if isinstance(instance, Student):
        return instance.admin
    if isinstance(instance, Teacher):
        return instance.teacher
    for relation_name in ('student', 'teacher', 'created_by'):
        related = getattr(instance, relation_name, None)
        if isinstance(related, Student):
            return related.admin
        if isinstance(related, Teacher):
            return related.teacher
    return None


def _write_log(action, instance, changes=None, description=None):
    actor, ip_address = _request_details()
    target = _target_user(instance)
    actor_reference = actor
    if action == 'delete' and actor and target and actor.pk == target.pk:
        actor_reference = None
    model_name = instance._meta.verbose_name.title()
    ActivityLog.objects.create(
        actor=actor_reference,
        actor_username=(actor.get_username() or '') if actor else '',
        target_user=target if action != 'delete' else None,
        target_username=(target.get_username() or '') if target else '',
        target_user_id_snapshot=str(target.pk) if target else '',
        action=action,
        object_type=instance._meta.label_lower,
        object_id=str(instance.pk or ''),
        description=(description or f'{model_name} {instance}')[:255],
        changes=changes or {},
        ip_address=ip_address,
    )


def _old_field_values(instance):
    if not instance.pk:
        return None
    return instance.__class__.objects.filter(pk=instance.pk).first()


def _capture_old_values(sender, instance, **kwargs):
    instance._activity_old_values = _old_field_values(instance)


def _record_save(sender, instance, created, **kwargs):
    old_instance = getattr(instance, '_activity_old_values', None)
    changes = {}
    for field in instance._meta.concrete_fields:
        old_value = (
            None if created else _field_value(old_instance, field)
        )
        new_value = _field_value(instance, field)
        if created or old_value != new_value:
            changes[field.name] = {
                'old': old_value,
                'new': new_value,
            }
    _write_log('create' if created else 'update', instance, changes)


def _record_delete(sender, instance, **kwargs):
    _write_log('delete', instance)


for _model in _TRACKED_MODELS:
    pre_save.connect(
        _capture_old_values,
        sender=_model,
        dispatch_uid=f'sims_activity_pre_save_{_model._meta.label_lower}',
    )
    post_save.connect(
        _record_save,
        sender=_model,
        dispatch_uid=f'sims_activity_post_save_{_model._meta.label_lower}',
    )
    post_delete.connect(
        _record_delete,
        sender=_model,
        dispatch_uid=f'sims_activity_post_delete_{_model._meta.label_lower}',
    )


def _record_auth_event(action, user, request=None, description=None):
    if not user:
        return
    if request is not None:
        actor = user if action != 'failed_sign_in' else None
        ip_address = request.META.get('REMOTE_ADDR')
    else:
        actor, ip_address = _request_details()
        if action == 'failed_sign_in':
            actor = None
    ActivityLog.objects.create(
        actor=actor,
        actor_username=user.get_username() or '',
        target_user=user,
        target_username=user.get_username() or '',
        target_user_id_snapshot=str(user.pk),
        action=action,
        object_type='authentication',
        object_id=str(user.pk),
        description=(
            description or
            f'{user.get_username()} {action.replace("_", " ")}'
        )[:255],
        changes={},
        ip_address=ip_address,
    )


@receiver(user_logged_in, dispatch_uid='sims_activity_user_logged_in')
def record_sign_in(sender, request, user, **kwargs):
    _record_auth_event('sign_in', user, request=request)


@receiver(user_logged_out, dispatch_uid='sims_activity_user_logged_out')
def record_sign_out(sender, request, user, **kwargs):
    _record_auth_event('sign_out', user, request=request)


def record_failed_attempt(request, username):
    username = username if isinstance(username, str) else ''
    user_model = get_user_model()
    user = user_model.objects.filter(
        username__iexact=username).first() if username else None
    if (
        user is None
        and getattr(settings, 'SIMS_ALLOW_EMAIL_SIGNIN', True)
        and '@' in username
    ):
        user = user_model.objects.filter(
            email__iexact=username,
            username__isnull=True,
        ).first()
    ActivityLog.objects.create(
        target_user=user,
        target_username=(user.get_username() or '') if user else '',
        target_user_id_snapshot=str(user.pk) if user else '',
        actor_username=username[:150],
        action='failed_sign_in',
        object_type='authentication',
        object_id=str(user.pk) if user else '',
        description=(
            f'Failed sign-in attempt for {username or "unknown user"}'
        )[:255],
        changes={},
        ip_address=request.META.get('REMOTE_ADDR') if request else None,
    )


@receiver(user_login_failed, dispatch_uid='sims_activity_failed_sign_in')
def record_failed_sign_in(sender, credentials, request, **kwargs):
    record_failed_attempt(request, credentials.get('username') or '')
