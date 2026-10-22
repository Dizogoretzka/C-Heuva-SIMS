import hashlib
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render, reverse
from django.utils import timezone

from .forms import (
    AdministratorRegistrationForm,
    LearnerRegistrationForm,
    TeacherRegistrationForm,
)
from .credentials import issue_pin, verify_pin
from .models import Admin, CustomUser, Student, Teacher
from .UsernameBackend import UsernameBackend


# Create your views here.

REGISTRATION_LIMIT = 5
REGISTRATION_WINDOW = 15 * 60


def render_pin_confirmation(
    request,
    role,
    pin,
    return_url=None,
    return_label='Continue to sign in',
    pin_title=None,
):
    response = render(request, 'main_app/pin_issued.html', {
        'selected_role': role,
        'issued_pin': pin,
        'return_url': return_url or reverse('role_login', args=[role]),
        'return_label': return_label,
        'pin_title': pin_title or f'{role.title()} account created',
    })
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
    response['Pragma'] = 'no-cache'
    return response


def update_profile_from_form(user, form):
    user.first_name = form.cleaned_data['first_name']
    user.last_name = form.cleaned_data['last_name']
    profile_picture = form.cleaned_data.get('profile_pic')
    if profile_picture:
        user.profile_pic = profile_picture

    new_password = form.cleaned_data.get('new_password')
    if new_password:
        user.set_password(new_password)
        user.failed_login_attempts = 0
        user.locked_until = None

    issued_pin = None
    with transaction.atomic():
        user.save()
        if form.cleaned_data.get('reset_pin'):
            issued_pin = issue_pin(user)
    return new_password, issued_pin


def _login_ip_cache_key(request):
    remote_address = request.META.get('REMOTE_ADDR', '')
    address_hash = hashlib.sha256(remote_address.encode()).hexdigest()
    return f'sims-login-failures:{address_hash}'


def _login_ip_limit_reached(request):
    return cache.get(_login_ip_cache_key(request), 0) >= (
        settings.SIMS_LOGIN_IP_MAX_FAILURES)


def _record_login_ip_failure(request):
    cache_key = _login_ip_cache_key(request)
    if cache.add(
        cache_key,
        1,
        timeout=settings.SIMS_LOGIN_IP_WINDOW_SECONDS,
    ):
        return
    try:
        cache.incr(cache_key)
    except ValueError:
        cache.add(
            cache_key,
            1,
            timeout=settings.SIMS_LOGIN_IP_WINDOW_SECONDS,
        )


def _record_account_login_failure(user, now):
    with transaction.atomic():
        account = CustomUser.objects.select_for_update().get(pk=user.pk)
        if account.locked_until and account.locked_until > now:
            return
        if account.locked_until:
            account.failed_login_attempts = 0
            account.locked_until = None
        account.failed_login_attempts += 1
        if account.failed_login_attempts >= settings.SIMS_LOGIN_MAX_FAILED_ATTEMPTS:
            account.locked_until = now + timedelta(
                minutes=settings.SIMS_LOGIN_LOCKOUT_MINUTES)
        account.save(update_fields=[
            'failed_login_attempts',
            'locked_until',
        ])


def login_page(request):
    if request.user.is_authenticated:
        if request.user.user_type == '1':
            return redirect(reverse("admin_home"))
        if request.user.user_type == '2':
            return redirect(reverse("student_home"))
        if request.user.user_type == '3':
            return redirect(reverse("teacher_home"))
    return render(request, 'main_app/landing.html')


def role_login(request, role):
    if role not in ('admin', 'teacher', 'learner'):
        return redirect(reverse('login_page'))
    if request.user.is_authenticated:
        if request.user.user_type == '1':
            return redirect(reverse('admin_home'))
        if request.user.user_type == '2':
            return redirect(reverse('student_home'))
        if request.user.user_type == '3':
            return redirect(reverse('teacher_home'))
    return render(request, 'main_app/login.html', {'selected_role': role})


def register_role(request, role):
    if role not in ('admin', 'teacher', 'learner'):
        return redirect(reverse('login_page'))
    if request.user.is_authenticated:
        if request.user.user_type == '1':
            return redirect(reverse('admin_home'))
        if request.user.user_type == '2':
            return redirect(reverse('student_home'))
        if request.user.user_type == '3':
            return redirect(reverse('teacher_home'))

    form_class = {
        'admin': AdministratorRegistrationForm,
        'teacher': TeacherRegistrationForm,
        'learner': LearnerRegistrationForm,
    }[role]
    form = form_class(request.POST or None, request.FILES or None)
    has_grades = getattr(form, 'has_grades', True)
    if request.method == 'POST':
        if _registration_rate_limited(request):
            messages.error(
                request,
                'Too many registration attempts. Please wait 15 minutes and try again.',
            )
        elif role == 'learner' and not has_grades:
            messages.error(
                request,
                'Learner registration is unavailable until the school adds grades.',
            )
        elif form.is_valid():
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']
            user_type = {'admin': '1', 'learner': '2', 'teacher': '3'}[role]
            profile_picture = form.cleaned_data.get('profile_pic')
            try:
                with transaction.atomic():
                    user = CustomUser.objects.create_user(
                        username=username,
                        password=password,
                        email='',
                        user_type=user_type,
                        first_name=form.cleaned_data['first_name'],
                        last_name=form.cleaned_data['last_name'],
                        gender=form.cleaned_data['gender'],
                        address='',
                        profile_pic=profile_picture or '',
                    )
                    if role == 'learner':
                        student = Student.objects.get(admin=user)
                        student.course = form.cleaned_data['course']
                        student.session = None
                        student.save(update_fields=['course', 'session'])
                    elif role == 'admin':
                        Admin.objects.get(admin=user)
                    else:
                        Teacher.objects.get(teacher=user)
                    pin = issue_pin(user)
            except IntegrityError:
                form.add_error(
                    'username',
                    'An account with this username already exists.',
                )
            else:
                return render_pin_confirmation(request, role, pin)

    return render(request, 'main_app/register.html', {
        'form': form,
        'selected_role': role,
        'has_grades': has_grades,
    })


def _registration_rate_limited(request):
    remote_address = request.META.get('REMOTE_ADDR', '')
    address_hash = hashlib.sha256(remote_address.encode()).hexdigest()
    cache_key = f'registration-attempts:{address_hash}'
    if cache.add(cache_key, 1, timeout=REGISTRATION_WINDOW):
        attempt_count = 1
    else:
        try:
            attempt_count = cache.incr(cache_key)
        except ValueError:
            cache.add(cache_key, 1, timeout=REGISTRATION_WINDOW)
            attempt_count = 1
    return attempt_count > REGISTRATION_LIMIT


def doLogin(request, **kwargs):
    if request.method != 'POST':
        return HttpResponse("<h4>Denied</h4>")
    role = request.POST.get('role')
    if role not in ('admin', 'teacher', 'learner'):
        return redirect(reverse('login_page'))
    login_url = reverse('role_login', args=[role])
    expected_user_type = {'admin': '1', 'learner': '2', 'teacher': '3'}[role]
    identifier = request.POST.get('username', '')
    password = request.POST.get('password', '')
    pin = request.POST.get('pin', '')
    backend = UsernameBackend()
    account = backend.get_user_for_identifier(identifier)
    authenticated_user = authenticate(
        request,
        username=identifier,
        password=password,
    )
    now = timezone.now()
    account_locked = False
    if account and account.locked_until:
        if account.locked_until > now:
            account_locked = True
        else:
            account.failed_login_attempts = 0
            account.locked_until = None
            account.save(update_fields=[
                'failed_login_attempts',
                'locked_until',
            ])

    ip_limited = _login_ip_limit_reached(request)
    identity_valid = bool(
        account
        and authenticated_user
        and authenticated_user.pk == account.pk
    )
    role_valid = bool(
        identity_valid and account.user_type == expected_user_type)
    has_pin = bool(account and account.pin_hash)
    pin_valid = verify_pin(pin, account.pin_hash if has_pin else '')
    pin_policy_valid = (
        pin_valid if has_pin else (
            not settings.SIMS_PIN_ENFORCED and not pin)
    )

    if (
        not identity_valid
        or not role_valid
        or not pin_policy_valid
        or account_locked
        or ip_limited
    ):
        if not ip_limited:
            _record_login_ip_failure(request)
            if account and not account_locked:
                _record_account_login_failure(account, now)
        messages.error(request, 'Invalid sign-in details')
        return redirect(login_url)

    account.failed_login_attempts = 0
    account.locked_until = None
    account.save(update_fields=['failed_login_attempts', 'locked_until'])
    login(request, authenticated_user)
    if role == 'admin':
        return redirect(reverse("admin_home"))
    if role == 'teacher':
        return redirect(reverse("teacher_home"))
    return redirect(reverse("student_home"))



def logout_user(request):
    if request.user != None:
        logout(request)
    return redirect("/")


def showFirebaseJS(request):
    data = """
    // Give the service worker access to Firebase Messaging.
// Note that you can only use Firebase Messaging here, other Firebase libraries
// are not available in the service worker.
importScripts('https://www.gstatic.com/firebasejs/7.22.1/firebase-app.js');
importScripts('https://www.gstatic.com/firebasejs/7.22.1/firebase-messaging.js');

// Initialize the Firebase app in the service worker by passing in
// your app's Firebase config object.
// https://firebase.google.com/docs/web/setup#config-object
firebase.initializeApp({
    apiKey: "AIzaSyBarDWWHTfTMSrtc5Lj3Cdw5dEvjAkFwtM",
    authDomain: "sms-with-django.firebaseapp.com",
    databaseURL: "https://sms-with-django.firebaseio.com",
    projectId: "sms-with-django",
    storageBucket: "sms-with-django.appspot.com",
    messagingSenderId: "945324593139",
    appId: "1:945324593139:web:03fa99a8854bbd38420c86",
    measurementId: "G-2F2RXTL9GT"
});

// Retrieve an instance of Firebase Messaging so that it can handle background
// messages.
const messaging = firebase.messaging();
messaging.setBackgroundMessageHandler(function (payload) {
    const notification = JSON.parse(payload);
    const notificationOption = {
        body: notification.body,
        icon: notification.icon
    }
    return self.registration.showNotification(payload.notification.title, notificationOption);
});
    """
    return HttpResponse(data, content_type='application/javascript')
