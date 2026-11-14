import csv
import re
import tempfile
from decimal import Decimal
from io import BytesIO, StringIO
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.contrib.auth.hashers import check_password
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.contrib.sessions.models import Session
from django.http import HttpResponse
from django.shortcuts import render
from django.test import RequestFactory, TestCase
from django.test.utils import override_settings
from django.templatetags.static import static
from django.urls import NoReverseMatch, Resolver404, resolve, reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format
from PIL import Image

from .models import (
    Admin,
    ActivityLog,
    Assessment,
    AssessmentPlan,
    SchoolClass,
    CustomUser,
    LoginActivity,
    AcademicPeriod,
    LearnerImportBatch,
    LearnerTeacherRequest,
    NotificationStudent,
    ReportCardPublication,
    RecycleBinEntry,
    SchoolProfile,
    Student,
    StudentResult,
    Subject,
    Teacher,
    TeacherClassSetup,
)
from .class_parser import ClassLabelError, parse_class_label
from .credentials import generate_pin, issue_pin, validate_pin
from .views import render_pin_confirmation
from .activity import reset_current_request, set_current_request
from .teacher_requests import create_class_requests
from .recycle_bin import (
    permanently_delete_entry,
    restore_entry,
    trash_queryset,
)


def accept_learner_for_teacher(student, teacher):
    if student.school_class_id:
        TeacherClassSetup.objects.get_or_create(
            teacher=teacher,
            school_class_id=student.school_class_id,
        )
    create_class_requests(student, accepted_teacher=teacher)


class RecycleBinTests(TestCase):
    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='bin-admin',
            password='valid-password',
            user_type='1',
            gender='M',
        )
        self.learner = CustomUser.objects.create_user(
            username='bin-learner',
            password='valid-password',
            user_type='2',
            gender='F',
            first_name='Bin',
            last_name='Learner',
        )

    def test_related_records_are_removed_and_restored_as_a_group(self):
        student = self.learner.student
        notification = NotificationStudent.objects.create(
            student=student,
            message='A related notification',
        )

        entry = trash_queryset(
            CustomUser.objects.filter(pk=self.learner.pk),
            self.admin,
            'learners',
            'Bin, Learner',
        )

        self.assertFalse(CustomUser.objects.filter(pk=self.learner.pk).exists())
        self.assertFalse(
            NotificationStudent.objects.filter(pk=notification.pk).exists())
        self.assertEqual(entry.object_count, 3)

        restore_entry(entry)

        self.assertTrue(CustomUser.objects.filter(pk=self.learner.pk).exists())
        self.assertTrue(Student.objects.filter(pk=student.pk).exists())
        self.assertEqual(
            NotificationStudent.objects.get(pk=notification.pk).message,
            'A related notification',
        )
        self.assertFalse(RecycleBinEntry.objects.filter(pk=entry.pk).exists())

    def test_bulk_delete_uses_search_scope_and_keeps_other_learners(self):
        matched = CustomUser.objects.create_user(
            username='matched-learner',
            password='valid-password',
            user_type='2',
            gender='F',
            first_name='Filter',
            last_name='Match',
        )
        other = CustomUser.objects.create_user(
            username='other-learner',
            password='valid-password',
            user_type='2',
            gender='F',
            first_name='Filter',
            last_name='Other',
        )
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse('bulk_delete_records', args=['learners']),
            {'search': 'Match'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(CustomUser.objects.filter(pk=matched.pk).exists())
        self.assertTrue(CustomUser.objects.filter(pk=other.pk).exists())
        self.assertTrue(
            RecycleBinEntry.objects.filter(
                owner=self.admin, record_type='learners').exists())

    def test_recycle_bin_is_private_to_its_owner(self):
        entry = trash_queryset(
            CustomUser.objects.filter(pk=self.learner.pk),
            self.admin,
            'learners',
            'Private learner',
        )
        other_user = CustomUser.objects.create_user(
            username='other-bin-user',
            password='valid-password',
            user_type='3',
            gender='M',
        )
        self.client.force_login(other_user)

        response = self.client.get(reverse('recycle_bin'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Private learner')

        response = self.client.post(
            reverse('restore_recycle_bin_entry', args=[entry.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(RecycleBinEntry.objects.filter(pk=entry.pk).exists())

    def test_restore_reinstates_relations_cleared_by_account_deletion(self):
        teacher = CustomUser.objects.create_user(
            username='bin-teacher',
            password='valid-password',
            user_type='3',
            gender='M',
        )
        subject = Subject.objects.create(
            name='Recycle bin subject',
            teacher=teacher.teacher,
            created_by=teacher.teacher,
        )

        entry = trash_queryset(
            CustomUser.objects.filter(pk=teacher.pk),
            self.admin,
            'teachers',
            'Recycle bin teacher',
        )

        subject.refresh_from_db()
        self.assertIsNone(subject.teacher)
        self.assertIsNone(subject.created_by)

        restore_entry(entry)

        subject.refresh_from_db()
        self.assertEqual(subject.teacher.teacher_id, teacher.pk)
        self.assertEqual(subject.created_by.teacher_id, teacher.pk)

    def test_permanent_deletion_removes_unreferenced_profile_media(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                image_data = BytesIO()
                Image.new('RGB', (1, 1), color='white').save(
                    image_data, format='PNG')
                self.learner.profile_pic.save(
                    'recycle-profile.png',
                    SimpleUploadedFile(
                        'recycle-profile.png', image_data.getvalue(),
                        content_type='image/png'),
                    save=True,
                )
                image_path = Path(self.learner.profile_pic.path)
                entry = trash_queryset(
                    CustomUser.objects.filter(pk=self.learner.pk),
                    self.admin,
                    'learners',
                    'Recycle profile',
                )
                self.assertTrue(image_path.exists())

                permanently_delete_entry(entry)

                self.assertFalse(image_path.exists())


class SessionInactivityTests(TestCase):
    def setUp(self):
        self.learner = CustomUser.objects.create_user(
            username='session-timeout-learner',
            password='valid-password',
            user_type='2',
            gender='F',
        )
        self.client.force_login(self.learner)

    def test_authenticated_requests_refresh_a_five_minute_idle_session(self):
        from django.conf import settings

        self.assertEqual(settings.SESSION_COOKIE_AGE, 86400)
        self.assertTrue(settings.SESSION_SAVE_EVERY_REQUEST)
        self.assertEqual(settings.SIMS_INACTIVITY_TIMEOUT_SECONDS, 300)

        self.client.get(reverse('student_home'))
        session_key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        first_expiry = Session.objects.get(
            session_key=session_key).expire_date

        self.client.get(reverse('student_home'))
        refreshed_expiry = Session.objects.get(
            session_key=session_key).expire_date

        self.assertGreater(refreshed_expiry, first_expiry)
        self.assertEqual(
            self.client.cookies[settings.SESSION_COOKIE_NAME]['max-age'],
            86400,
        )

    def test_expired_session_is_logged_out_on_the_next_request(self):
        from django.conf import settings

        self.client.get(reverse('student_home'))
        session_key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        Session.objects.filter(session_key=session_key).update(
            expire_date=timezone.now() - timedelta(seconds=1))

        response = self.client.get(reverse('student_home'))

        self.assertRedirects(
            response,
            reverse('login_page'),
            fetch_redirect_response=False,
        )
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_five_minutes_of_inactivity_preserves_saved_data_and_shows_message(self):
        self.client.get(reverse('student_home'))
        session = self.client.session
        session['sims_last_activity'] = (
            timezone.now() - timedelta(minutes=5, seconds=1)
        ).timestamp()
        session.save()

        response = self.client.get(
            reverse('student_home'),
            follow=True,
        )

        self.assertContains(response, 'inactive for 5 minutes')
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertTrue(CustomUser.objects.filter(pk=self.learner.pk).exists())

    def test_configured_timezone_is_namibia_central_africa_time(self):
        from django.conf import settings

        self.assertEqual(settings.TIME_ZONE, 'Africa/Windhoek')
        self.assertEqual(
            timezone.localtime(timezone.now()).utcoffset(),
            timedelta(hours=2),
        )


@override_settings(PASSWORD_HASHERS=[
    'django.contrib.auth.hashers.MD5PasswordHasher',
])
class AdminAccountManagementTests(TestCase):
    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='account-admin',
            password='valid-password',
            user_type='1',
            gender='F',
        )
        self.client.force_login(self.admin)

    def test_admin_can_create_another_admin_with_generated_id_and_one_time_pin(self):
        response = self.client.post(reverse('manage_admins'), {
            'first_name': 'Nambala',
            'last_name': 'Mbangula',
            'gender': 'F',
        })

        self.assertEqual(response.status_code, 200)
        account = CustomUser.objects.get(username='mbangulan')
        self.assertEqual(account.user_type, '1')
        self.assertTrue(Admin.objects.filter(admin=account).exists())
        self.assertTrue(
            check_password(response.context['issued_pin'], account.pin_hash))
        self.assertFalse(account.has_usable_password())

    def test_non_admin_cannot_create_an_administrator_account(self):
        teacher = CustomUser.objects.create_user(
            username='account-teacher',
            password='valid-password',
            user_type='3',
            gender='M',
        )
        self.client.force_login(teacher)

        response = self.client.get(reverse('manage_admins'))

        self.assertRedirects(
            response,
            reverse('teacher_home'),
            fetch_redirect_response=False,
        )


class CrestBrandingTests(TestCase):
    def _assert_page_branding(self, response):
        self.assertContains(
            response,
            f'src="{static("main_app/img/crest-logo.png")}"',
        )
        self.assertContains(
            response,
            f'href="{static("main_app/img/favicon.ico")}"',
        )
        self.assertContains(
            response,
            f'href="{static("main_app/img/favicon-32x32.png")}"',
        )
        self.assertContains(
            response,
            f'href="{static("main_app/img/favicon-192x192.png")}"',
        )
        self.assertContains(
            response,
            f'href="{static("main_app/img/apple-touch-icon.png")}"',
        )
        self.assertContains(response, 'id="page-loader"')
        self.assertContains(
            response,
            static('main_app/css/page-loader.css'),
        )
        self.assertContains(
            response,
            static('main_app/js/page-loader.js'),
        )

    def test_landing_sign_in_learner_registration_and_pin_pages_show_branding(self):
        self._assert_page_branding(self.client.get(reverse('login_page')))
        for role in ('admin', 'teacher', 'learner'):
            with self.subTest(role=role):
                self._assert_page_branding(
                    self.client.get(reverse('role_login', args=[role])))
        self._assert_page_branding(
            self.client.get(reverse('register_learner')))

        pin_response = render_pin_confirmation(
            RequestFactory().get('/'),
            'learner',
            '12345',
        )
        self._assert_page_branding(pin_response)
        account = CustomUser.objects.create_user(
            username='branding-account',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        reset_response = render(
            RequestFactory().get('/'),
            'main_app/credential_reset_confirmation.html',
            {
                'account': account,
                'secret': 'new-secret',
                'secret_label': 'New password',
                'return_url': '/',
            },
        )
        self._assert_page_branding(reset_response)

    def test_all_three_panels_show_the_crest_and_role_name(self):
        panels = (
            ('1', 'admin_home', 'Admin Panel', 'admin-branding'),
            ('3', 'teacher_home', 'Teacher Panel', 'teacher-branding'),
            ('2', 'student_home', 'Learner Panel', 'learner-branding'),
        )
        for role, page, panel_name, username in panels:
            with self.subTest(role=role):
                user = CustomUser.objects.create_user(
                    username=username,
                    password='valid-password',
                    user_type=role,
                    gender='F',
                    address='',
                )
                self.client.force_login(user)
                response = self.client.get(reverse(page))
                self.assertEqual(response.status_code, 200)
                self.assertContains(
                    response, 'alt="C. Heuva school crest"')
                self.assertContains(response, 'id="page-loader"')
                self.assertContains(response, panel_name)
                self.client.logout()

    @override_settings(DEBUG=True)
    def test_static_branding_assets_are_served_and_favicon_is_public(self):
        image_names = (
            'crest-logo.png',
            'favicon.ico',
            'favicon-32x32.png',
            'favicon-192x192.png',
            'apple-touch-icon.png',
        )
        for image_name in image_names:
            with self.subTest(image=image_name):
                url = static(f'main_app/img/{image_name}')
                response = self.client.get(url)
                self.assertEqual(
                    response.status_code, 200,
                    f'{url} resolved as {response.wsgi_request.resolver_match}')
                expected = Path(
                    'main_app/static/main_app/img', image_name).read_bytes()
                self.assertEqual(b''.join(response.streaming_content), expected)

        for asset in (
                'main_app/css/page-loader.css',
                'main_app/js/page-loader.js',
        ):
                with self.subTest(asset=asset):
                    response = self.client.get(static(asset))
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(b''.join(response.streaming_content))

        response = self.client.get('/favicon.ico', follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.redirect_chain[0][0],
            static('main_app/img/favicon.ico'),
        )
        self.assertEqual(
            b''.join(response.streaming_content),
            Path('main_app/static/main_app/img/favicon.ico').read_bytes(),
        )

    def test_reports_include_crest_and_still_render_without_it(self):
        admin = CustomUser.objects.create_user(
            username='crest-report-admin',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        from .hod_views import _create_report_pdf

        response = _create_report_pdf(
            admin,
            'Crest Test Report',
            ['Column'],
            [['Value']],
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'%PDF'))
        self.assertIn(b'/Subtype /Image', response.content)

        with tempfile.TemporaryDirectory() as static_root:
            with patch(
                    'main_app.hod_views.finders.find',
                    return_value=None), patch(
                    'main_app.hod_views.settings.STATIC_ROOT',
                    static_root):
                with self.assertLogs('main_app.hod_views', level='WARNING'):
                    response = _create_report_pdf(
                        admin,
                        'Report without crest',
                        ['Column'],
                        [['Value']],
                    )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'%PDF'))
        self.assertNotIn(b'/Subtype /Image', response.content)


class InterfaceSelectionTests(TestCase):
    @staticmethod
    def _role_markup(response, element, role):
        html = response.content.decode()
        if element == 'form':
            pattern = (
                rf'<form(?=[^>]*data-role-form="{role}")[^>]*>'
                rf'(.*?)</form>')
        else:
            pattern = (
                rf'<div(?=[^>]*data-role-info)'
                rf'(?=[^>]*data-role-panel="{role}")[^>]*>'
                rf'(.*?)</div>')
        match = re.search(pattern, html, re.DOTALL)
        if match is None:
            raise AssertionError(f'No {element} found for role {role}.')
        return match.group(1)

    @staticmethod
    def _profile_image_upload():
        image_data = BytesIO()
        Image.new('RGB', (1, 1), color='white').save(image_data, format='PNG')
        return SimpleUploadedFile(
            'profile.png', image_data.getvalue(), content_type='image/png')

    def test_landing_page_offers_all_three_interfaces(self):
        response = self.client.get(reverse('login_page'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Administrator')
        self.assertContains(response, 'Teacher')
        self.assertContains(response, 'Learner')
        self.assertContains(response, 'name="selected_role"')
        self.assertContains(response, 'value="learner" checked')
        self.assertContains(response, 'id="signin-title">Sign in')
        self.assertContains(response, 'name="account_id"')
        self.assertContains(response, 'Account ID')
        self.assertContains(response, 'name="password"')
        self.assertContains(response, 'name="pin"')
        self.assertContains(response, '<noscript>')
        self.assertEqual(
            response.context['today'],
            timezone.localdate(),
        )
        self.assertContains(
            response,
            date_format(timezone.localdate(), 'l, j F Y'),
        )
        self.assertContains(
            response,
            'src="%s"' % static('main_app/img/crest-logo.png'),
        )
        self.assertContains(
            response,
            'href="%s"' % static('main_app/img/favicon.ico'),
        )

    def test_each_role_has_a_login_page(self):
        for role in ('admin', 'teacher', 'learner'):
            with self.subTest(role=role):
                response = self.client.get(reverse('role_login', args=[role]))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.templates[0].name,
                    'main_app/landing.html',
                )
                self.assertEqual(response.context['selected_role'], role)
                self.assertContains(response, f'Sign in as {role.title()}')
                form = self._role_markup(response, 'form', role)
                if role == 'learner':
                    self.assertIn('name="password"', form)
                    self.assertNotIn('name="pin"', form)
                    self.assertContains(
                        response, "Don't have an account? Register")
                    self.assertContains(response, 'Choose a different interface')
                else:
                    self.assertIn('name="pin"', form)
                    self.assertNotIn('name="password"', form)
                    self.assertIn('inputmode="numeric"', form)
                    self.assertIn('maxlength="5"', form)
                    self.assertIn('5 numeric digits', form)
                    self.assertContains(response, 'Choose a different interface')
                    self.assertContains(response, 'data-toggle-pin')
                    self.assertContains(response, 'aria-label="Show PIN"')
                    info = self._role_markup(response, 'info', role)
                    self.assertNotIn('/register/', info)
                    self.assertNotIn('Register', info)

    def test_registration_link_is_only_in_learner_information_block(self):
        response = self.client.get(reverse('login_page'))
        learner_info = self._role_markup(response, 'info', 'learner')
        self.assertIn(reverse('register_learner'), learner_info)
        for role in ('admin', 'teacher'):
            with self.subTest(role=role):
                info = self._role_markup(response, 'info', role)
                self.assertNotIn(reverse('register_learner'), info)
                self.assertNotIn('Register', info)

    def test_all_role_forms_sign_in_through_the_existing_view(self):
        learner = CustomUser.objects.create_user(
            username='landing-learner',
            password='valid-password',
            user_type='2',
            gender='F',
        )
        admin = CustomUser.objects.create_user(
            username='landing-admin',
            user_type='1',
            gender='F',
        )
        teacher = CustomUser.objects.create_user(
            username='landing-teacher',
            user_type='3',
            gender='F',
        )
        accounts = (
            ('learner', learner, {'password': 'valid-password'}, 'student_home'),
            ('admin', admin, {'pin': issue_pin(admin)}, 'admin_home'),
            ('teacher', teacher, {'pin': issue_pin(teacher)}, 'teacher_home'),
        )
        for role, account, credential, destination in accounts:
            with self.subTest(role=role):
                response = self.client.post(reverse('user_login'), {
                    'role': role,
                    'username': account.username,
                    **credential,
                })
                self.assertRedirects(
                    response,
                    reverse(destination),
                    fetch_redirect_response=False,
                )
                self.client.logout()

    def test_failed_sign_in_restores_role_and_username_without_secret(self):
        learner = CustomUser.objects.create_user(
            username='landing-failed-learner',
            password='valid-password',
            user_type='2',
            gender='F',
        )
        secret_password = 'incorrect-password-value'
        secret_pin = '01234'
        response = self.client.post(reverse('user_login'), {
            'role': 'admin',
            'username': learner.username,
            'password': secret_password,
            'pin': secret_pin,
        })
        self.assertRedirects(
            response,
            reverse('role_login', args=['admin']),
            fetch_redirect_response=False,
        )

        page = self.client.get(reverse('role_login', args=['admin']))
        form = self._role_markup(page, 'form', 'admin')
        self.assertEqual(page.context['selected_role'], 'admin')
        self.assertIn('Invalid sign-in details', form)
        self.assertIn('role="alert"', form)
        self.assertIn('value="landing-failed-learner"', form)
        self.assertNotIn(secret_password, page.content.decode())
        self.assertNotIn(secret_pin, page.content.decode())

    def test_staff_roles_do_not_render_registration_in_their_role_blocks(self):
        page = self.client.get(reverse('login_page'))
        for role in ('admin', 'teacher'):
            with self.subTest(role=role):
                info = self._role_markup(page, 'info', role)
                self.assertNotIn('Register', info)
                self.assertNotIn('/register/', info)

    def test_learner_can_sign_in_through_learner_interface(self):
        learner = CustomUser.objects.create_user(
            email='learner@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )

        response = self.client.post(reverse('user_login'), {
            'username': 'learner@example.com',
            'password': 'valid-password',
            'role': 'learner',
        }, REMOTE_ADDR='203.0.113.12')

        self.assertRedirects(
            response,
            reverse('student_home'),
            fetch_redirect_response=False,
        )
        activity = LoginActivity.objects.get(student=learner.student)
        self.assertEqual(activity.ip_address, '203.0.113.12')
        self.assertFalse(learner.pin_hash)
        self.assertTrue(learner.check_password('valid-password'))

    def test_administrator_login_does_not_create_login_activity(self):
        admin = CustomUser.objects.create_user(
            username='admin-login',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        admin_pin = issue_pin(admin)

        response = self.client.post(reverse('user_login'), {
            'username': admin.username,
            'password': 'valid-password',
            'role': 'admin',
        })
        self.assertRedirects(
            response,
            reverse('role_login', args=['admin']),
            fetch_redirect_response=False,
        )
        response = self.client.post(reverse('user_login'), {
            'username': admin.username,
            'pin': admin_pin,
            'role': 'admin',
        })

        self.assertRedirects(
            response,
            reverse('admin_home'),
            fetch_redirect_response=False,
        )
        self.assertEqual(LoginActivity.objects.count(), 0)

    def test_failed_learner_login_does_not_create_login_activity(self):
        learner = CustomUser.objects.create_user(
            email='failed-login@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )

        response = self.client.post(reverse('user_login'), {
            'username': 'failed-login@example.com',
            'password': 'wrong-password',
            'role': 'learner',
        })

        self.assertRedirects(
            response,
            reverse('role_login', args=['learner']),
            fetch_redirect_response=False,
        )
        self.assertEqual(LoginActivity.objects.count(), 0)
        failure = ActivityLog.objects.get(
            action='failed_sign_in',
            object_type='authentication',
            target_user=learner,
        )
        self.assertNotIn('wrong-password', failure.description)
        self.assertEqual(failure.changes, {})

    def test_learner_profile_is_created_and_dashboard_loads(self):
        learner = CustomUser.objects.create_user(
            email='dashboard@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )

        self.client.force_login(learner)
        response = self.client.get(reverse('student_home'))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(learner.student.pk)

    def test_learner_dashboard_links_to_results_and_available_report_card(self):
        learner = CustomUser.objects.create_user(
            email='dashboard-report-card@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        teacher = CustomUser.objects.create_user(
            username='dashboard-report-teacher',
            password='valid-password',
            user_type='3',
            gender='M',
        )
        school_class = SchoolClass.objects.create(grade_number=8)
        TeacherClassSetup.objects.create(
            teacher=teacher.teacher, school_class=school_class)
        learner.student.school_class = school_class
        academic_period = AcademicPeriod.objects.create(
            start_year='2026-01-01', end_year='2026-12-31')
        learner.student.academic_period = academic_period
        learner.student.save()
        accept_learner_for_teacher(learner.student, teacher.teacher)
        subject = Subject.objects.create(
            name='Mathematics',
            school_class=school_class,
            teacher=teacher.teacher,
        )
        StudentResult.objects.create(
            student=learner.student,
            subject=subject,
            academic_period=academic_period,
            test=20,
            exam=70)
        self.client.force_login(learner)

        response = self.client.get(reverse('student_home'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'My Results')
        self.assertContains(response, reverse('student_view_result'))
        self.assertContains(response, 'Download report card')
        self.assertContains(response, reverse('student_report_card'))

    def test_learner_dashboard_shows_report_card_availability_when_no_results(self):
        learner = CustomUser.objects.create_user(
            email='dashboard-no-report@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        self.client.force_login(learner)

        response = self.client.get(reverse('student_home'))

        self.assertContains(response, 'My Report Card')
        self.assertContains(
            response,
            'Available after an academic period / term and results are assigned.',
        )
        self.assertNotContains(response, reverse('student_report_card'))

    def test_learner_dashboard_loads_without_attendance_features(self):
        learner = CustomUser.objects.create_user(
            email='unassigned@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        self.client.force_login(learner)

        response = self.client.get(reverse('student_home'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'View Attendance')
        self.assertNotContains(response, 'Total Attendance')

    def test_learner_sees_only_subjects_assigned_to_their_class(self):
        learner = CustomUser.objects.create_user(
            email='subjects-learner@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        learner_class = SchoolClass.objects.create(grade_number=7)
        other_class = SchoolClass.objects.create(grade_number=8)
        learner.student.school_class = learner_class
        learner.student.save()
        teacher_user = CustomUser.objects.create_user(
            username='subjects-teacher',
            password='valid-password',
            user_type='3',
            gender='M',
        )
        TeacherClassSetup.objects.create(
            teacher=teacher_user.teacher, school_class=learner_class)
        accept_learner_for_teacher(learner.student, teacher_user.teacher)
        Subject.objects.create(
            name='Mathematics',
            school_class=learner_class,
            teacher=teacher_user.teacher,
        )
        other_teacher = CustomUser.objects.create_user(
            username='subjects-other-teacher',
            password='valid-password',
            user_type='3',
            gender='F',
        )
        TeacherClassSetup.objects.create(
            teacher=other_teacher.teacher, school_class=learner_class)
        Subject.objects.create(
            name='Geography',
            school_class=learner_class,
            teacher=other_teacher.teacher,
        )
        Subject.objects.create(
            name='History', school_class=other_class, teacher=teacher_user.teacher)
        self.client.force_login(learner)

        response = self.client.get(reverse('student_view_subjects'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Mathematics')
        self.assertNotContains(response, 'History')
        self.assertNotContains(response, 'Geography')
        self.assertNotContains(response, 'Add Subject')

    def test_administrator_cannot_manage_subjects(self):
        admin = CustomUser.objects.create_user(
            email='subject-author@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        learner = CustomUser.objects.create_user(
            email='subject-viewer@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        self.client.force_login(admin)
        for path in ('/subject/add/', '/subject/manage/'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

        self.client.force_login(learner)
        learner_response = self.client.get(reverse('student_view_subjects'))

        self.assertEqual(learner_response.status_code, 200)
        self.assertNotContains(learner_response, 'Science')

    def test_learner_subject_page_shows_message_when_no_subjects_are_assigned(self):
        learner = CustomUser.objects.create_user(
            email='no-subjects-learner@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        self.client.force_login(learner)

        response = self.client.get(reverse('student_view_subjects'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'No subjects are available from your accepted teachers yet.',
        )

    def test_admin_dashboard_excludes_teacher_managed_tools(self):
        admin = CustomUser.objects.create_user(
            email='dashboard-admin@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        self.client.force_login(admin)

        response = self.client.get(reverse('admin_home'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Grade Bands')
        self.assertNotContains(response, 'Learner Report Cards')
        self.assertNotContains(response, reverse('generate_report_card'))
        for label in (
            'Add Course',
            'Manage Course',
            'Add Academic Period',
            'Manage Academic Period',
        ):
            with self.subTest(label=label):
                self.assertNotContains(response, label)

    def test_manage_students_has_only_add_student_modal(self):
        admin = CustomUser.objects.create_user(
            email='add-student-admin@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        CustomUser.objects.create_user(
            username='listed-learner',
            email='legacy-learner@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        self.client.force_login(admin)

        response = self.client.get(reverse('manage_student'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-target="#addStudentModal"')
        self.assertContains(response, 'action="' + reverse('add_student') + '"')
        self.assertNotContains(response, 'addSubjectModal')
        self.assertContains(response, 'id_grade_and_class')
        self.assertContains(response, 'id_academic_period')
        self.assertContains(response, 'Add Learner')
        self.assertNotContains(response, 'Add Student')
        self.assertNotContains(response, 'Add Subject</p>')
        self.assertContains(response, 'listed-learner')
        self.assertNotContains(response, 'legacy-learner@example.com')

    def test_address_and_profile_picture_are_optional_in_staff_forms(self):
        from .forms import StudentForm, TeacherAccountForm

        student_form = StudentForm()
        teacher_form = TeacherAccountForm()

        self.assertFalse(student_form.fields['address'].required)
        self.assertFalse(student_form.fields['profile_pic'].required)
        self.assertFalse(teacher_form.fields['profile_pic'].required)

    def test_manage_students_restores_missing_learner_profile(self):
        admin = CustomUser.objects.create_user(
            email='missing-profile-admin@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        learner = CustomUser.objects.create_user(
            email='missing-profile-learner@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        learner.student.delete()
        self.client.force_login(admin)

        response = self.client.get(reverse('manage_student'))

        self.assertEqual(response.status_code, 200)
        learner_profile = Student.objects.get(admin=learner)
        self.assertContains(
            response, reverse('edit_student', args=[learner_profile.id]))
        self.assertTrue(Student.objects.filter(admin=learner).exists())

    def test_add_learner_page_redirects_and_subject_urls_are_removed(self):
        admin = CustomUser.objects.create_user(
            email='add-subject-admin@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        self.client.force_login(admin)

        student_response = self.client.get(reverse('add_student'))

        self.assertRedirects(
            student_response, reverse('manage_student'),
            fetch_redirect_response=False)
        self.assertEqual(self.client.get('/subject/add/').status_code, 404)
        self.assertEqual(self.client.get('/subject/manage/').status_code, 404)

    def test_navigation_shows_each_role_only_its_functions(self):
        admin = CustomUser.objects.create_user(
            email='report-sidebar-admin@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        learner = CustomUser.objects.create_user(
            email='report-sidebar-learner@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        teacher = CustomUser.objects.create_user(
            email='report-sidebar-teacher@example.com',
            password='valid-password',
            user_type='3',
            gender='F',
            address='',
        )

        self.client.force_login(admin)
        admin_response = self.client.get(reverse('admin_home'))
        self.assertContains(admin_response, 'School Summary / Class List')
        self.assertNotContains(admin_response, 'Manage Results')
        self.assertNotContains(admin_response, reverse('manage_student_results'))
        self.assertNotContains(admin_response, 'Grade Bands')
        self.assertNotContains(admin_response, reverse('generate_report_card'))
        self.assertContains(
            admin_response, reverse('generate_report'), html=False)
        admin_html = admin_response.content.decode()
        self.assertLess(
            admin_html.index('School Summary / Class List'),
            admin_html.index('Logout'),
        )
        self.assertIn('fas fa-home', admin_html)
        self.assertIn('fas fa-user-edit', admin_html)
        self.assertIn('fas fa-calendar-minus', admin_html)
        self.assertIn('fas fa-comments', admin_html)

        self.client.force_login(learner)
        learner_response = self.client.get(reverse('student_home'))
        self.assertNotContains(learner_response, 'Generate Report')
        learner_html = learner_response.content.decode()
        self.assertIn('fas fa-home', learner_html)
        self.assertIn('fas fa-user-edit', learner_html)
        self.assertIn('fas fa-calendar-minus', learner_html)
        self.assertIn('fas fa-comments', learner_html)

        self.client.force_login(teacher)
        teacher_response = self.client.get(reverse('teacher_home'))
        self.assertContains(teacher_response, 'Manage Results')
        self.assertNotContains(teacher_response, 'Grade Bands')
        self.assertNotContains(teacher_response, reverse('manage_teachers'))
        self.assertNotContains(teacher_response, reverse('learner_login_activity'))

    def test_admin_search_bars_render_on_requested_pages(self):
        admin = CustomUser.objects.create_user(
            email='search-admin@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        self.client.force_login(admin)
        pages = (
            ('admin_notify_student', 'notify-student-list'),
            ('manage_student', 'manage-student-list'),
            ('student_feedback_message', 'admin-feedback-list'),
            ('view_student_leave', 'admin-leave-list'),
        )
        for page_name, target_id in pages:
            with self.subTest(page=page_name):
                response = self.client.get(reverse(page_name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(
                    response, f'data-search-target="#{target_id}"')

    def test_learner_search_bars_render_on_requested_pages(self):
        learner = CustomUser.objects.create_user(
            email='search-learner@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        self.client.force_login(learner)
        pages = (
            ('student_view_subjects', 'learner-subject-list'),
            ('student_view_result', 'learner-results-list'),
            ('student_view_notification', 'learner-notifications-list'),
            ('student_apply_leave', 'learner-leave-list'),
            ('student_feedback', 'learner-feedback-list'),
        )
        for page_name, target_id in pages:
            with self.subTest(page=page_name):
                response = self.client.get(reverse(page_name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(
                    response, f'data-search-target="#{target_id}"')

    def test_learner_terminology_is_used_in_role_and_admin_labels(self):
        admin = CustomUser.objects.create_user(
            email='learner-terminology-admin@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        self.client.force_login(admin)
        response = self.client.get(reverse('admin_home'))

        self.assertContains(response, 'Manage Learners')
        self.assertContains(response, 'Total Learners')
        self.assertEqual(str(Student._meta.verbose_name), 'learner')
        self.assertEqual(
            str(StudentResult._meta.get_field('student').verbose_name),
            'learner')
        self.assertEqual(CustomUser.USER_TYPE[1][1], 'Learner')

    def test_removed_management_routes_are_not_registered(self):
        removed_paths = (
            '/course/add',
            '/course/manage/',
            '/add_session/',
            '/academic_period/manage/',
            '/attendance/view/',
            '/attendance/fetch/',
            '/get_attendance',
            '/student/view/attendance/',
        )
        for path in removed_paths:
            with self.subTest(path=path):
                with self.assertRaises(Resolver404):
                    resolve(path)
        for path in ('/course/add', '/course/manage/'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_attendance_urls_return_404_and_are_not_linked(self):
        admin = CustomUser.objects.create_user(
            username='attendance-route-admin',
            password='valid-password',
            user_type='1',
            gender='F',
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_login(admin)
        self.assertEqual(self.client.get('/admin/attendance/').status_code, 404)
        self.client.logout()

        for path in (
                '/teacher/attendance/',
                '/student/attendance/'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)

        user = CustomUser.objects.create_user(
            username='no-attendance-links',
            password='valid-password',
            user_type='2',
            gender='F',
        )
        self.client.force_login(user)
        response = self.client.get(reverse('student_home'))
        self.assertNotContains(response, '/student/attendance/')
        self.assertNotContains(response, 'My Attendance')

    def test_student_and_subject_forms_support_report_class_filters(self):
        from django.apps import apps
        from .forms import StudentForm

        self.assertIn('grade_and_class', StudentForm().fields)
        self.assertIn('academic_period', StudentForm().fields)
        self.assertIn(
            'school_class', [field.name for field in Subject._meta.fields])
        self.assertNotIn('course', StudentForm().fields)
        self.assertNotIn(
            'course', [field.name for field in Subject._meta.fields])
        with self.assertRaises(LookupError):
            apps.get_model('main_app', 'Course')

    def test_attendance_models_and_reports_are_removed(self):
        from django.apps import apps
        from .forms import ReportGenerationForm

        for model_name in ('Attendance', 'AttendanceReport'):
            with self.subTest(model=model_name):
                with self.assertRaises(LookupError):
                    apps.get_model('main_app', model_name)
        for role in ('admin', 'teacher'):
            report_types = {
                value for value, _ in ReportGenerationForm(
                    role=role).fields['report_type'].choices
            }
            self.assertNotIn('attendance_summary', report_types)

    def test_legacy_learner_login_activity_url_redirects_to_activity_log(self):
        admin = CustomUser.objects.create_user(
            email='activity-admin@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        learner = CustomUser.objects.create_user(
            email='activity-learner@example.com',
            username='activity-learner',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
            first_name='Activity',
            last_name='Learner',
        )
        school_class = SchoolClass.objects.create(grade_number=7)
        learner.student.school_class = school_class
        learner.student.save()
        LoginActivity.objects.create(
            student=learner.student, ip_address='203.0.113.7')
        self.client.force_login(admin)

        response = self.client.get(reverse('learner_login_activity'))

        self.assertRedirects(
            response, reverse('activity_log'),
            fetch_redirect_response=False)
        response = self.client.get(reverse('activity_log'), {
            'search': learner.username,
            'role': '2',
            'action': 'sign_in',
        })
        self.assertContains(response, 'Activity Learner')
        self.assertContains(response, learner.username)

    def test_learner_cannot_view_learner_login_activity(self):
        learner = CustomUser.objects.create_user(
            email='activity-viewer@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        self.client.force_login(learner)

        response = self.client.get(reverse('learner_login_activity'))

        self.assertRedirects(
            response,
            reverse('student_home'),
            fetch_redirect_response=False,
        )

    def test_legacy_signins_use_activity_log_search_and_date_filters(self):
        admin = CustomUser.objects.create_user(
            email='filter-admin@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        ada = CustomUser.objects.create_user(
            email='ada@example.com',
            username='ada-learner',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
            first_name='Ada',
            last_name='Okafor',
        )
        binta = CustomUser.objects.create_user(
            email='binta@example.com',
            username='binta-learner',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
            first_name='Binta',
            last_name='Bello',
        )
        grade_seven = SchoolClass.objects.create(grade_number=7)
        grade_eight = SchoolClass.objects.create(grade_number=8)
        ada.student.school_class = grade_seven
        ada.student.save()
        binta.student.school_class = grade_eight
        binta.student.save()
        LoginActivity.objects.create(student=ada.student)
        old_activity = LoginActivity.objects.create(student=binta.student)
        LoginActivity.objects.filter(pk=old_activity.pk).update(
            logged_in_at=timezone.now() - timedelta(days=1))
        self.client.force_login(admin)

        response = self.client.get(reverse('activity_log'), {
            'search': 'ada-learner',
            'start_date': timezone.localdate().isoformat(),
            'end_date': timezone.localdate().isoformat(),
            'role': '2',
            'action': 'sign_in',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ada.username)
        self.assertNotContains(response, binta.username)

        response = self.client.get(reverse('activity_log'), {
            'search': 'Binta',
            'role': '2',
            'action': 'sign_in',
        })
        self.assertContains(response, binta.username)
        self.assertNotContains(response, ada.username)

        response = self.client.get(reverse('activity_log'), {
            'start_date': (timezone.localdate() - timedelta(days=1)).isoformat(),
            'end_date': (timezone.localdate() - timedelta(days=1)).isoformat(),
            'role': '2',
            'action': 'sign_in',
        })
        self.assertContains(response, binta.username)
        self.assertNotContains(response, ada.username)

    def test_admin_profile_cannot_change_password_but_remains_editable(self):
        admin = CustomUser.objects.create_user(
            email='profile-admin@example.com',
            username='profile-admin',
            password='original-password',
            user_type='1',
            gender='F',
            address='Initial address',
            first_name='Profile',
            last_name='Admin',
        )
        self.client.force_login(admin)

        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                profile_url = reverse('admin_view_profile')
                first_update = self.client.post(profile_url, {
                    'first_name': 'Updated',
                    'last_name': 'Admin',
                    'current_password': 'original-password',
                    'new_password': 'new-unique-password-984!',
                    'confirm_password': 'new-unique-password-984!',
                    'profile_pic': self._profile_image_upload(),
                })

                self.assertRedirects(
                    first_update, profile_url, fetch_redirect_response=False)
                admin.refresh_from_db()
                self.assertTrue(admin.check_password('original-password'))
                self.assertFalse(
                    admin.check_password('new-unique-password-984!'))
                profile_response = self.client.get(profile_url)
                self.assertEqual(profile_response.status_code, 200)
                self.assertNotContains(profile_response, 'name="new_password"')
                self.assertContains(profile_response, 'name="current_pin"')
                self.assertContains(profile_response, 'name="reset_pin"')

                second_update = self.client.post(profile_url, {
                    'first_name': 'Updated Again',
                    'last_name': 'Admin',
                    'profile_pic': self._profile_image_upload(),
                })

        self.assertRedirects(
            second_update, profile_url, fetch_redirect_response=False)
        admin.refresh_from_db()
        self.assertEqual(admin.first_name, 'Updated Again')


@override_settings(PASSWORD_HASHERS=[
    'django.contrib.auth.hashers.MD5PasswordHasher',
])
class TeacherRoleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.teacher = CustomUser.objects.create_user(
            email='teacher-role@example.com',
            username='teacher-role',
            password='valid-password',
            user_type='3',
            gender='F',
            address='',
            first_name='Taylor',
            last_name='Teacher',
        )
        self.admin = CustomUser.objects.create_user(
            email='teacher-role-admin@example.com',
            username='teacher-role-admin',
            password='valid-password',
            user_type='1',
            gender='M',
            address='',
        )
        self.school_class = None
        self.academic_period = None
        self.subject = None

    def _set_up_class(self):
        if self.school_class is not None:
            return
        self.school_class = SchoolClass.objects.create(grade_number=8)
        self.academic_period = AcademicPeriod.objects.create(
            start_year='2026-01-01',
            end_year='2026-12-31',
        )
        TeacherClassSetup.objects.create(
            teacher=self.teacher.teacher,
            school_class=self.school_class,
        )
        self.subject = Subject.objects.create(
            name='Science',
            school_class=self.school_class,
            teacher=self.teacher.teacher,
        )

    def _learner(self, email, first_name):
        self._set_up_class()
        learner = CustomUser.objects.create_user(
            email=email,
            username=email.split('@', 1)[0],
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
            first_name=first_name,
            last_name='Learner',
        )
        learner.student.school_class = self.school_class
        learner.student.academic_period = self.academic_period
        learner.student.save()
        accept_learner_for_teacher(learner.student, self.teacher.teacher)
        return learner

    @staticmethod
    def _image():
        image_data = BytesIO()
        Image.new('RGB', (1, 1), color='white').save(image_data, format='PNG')
        return SimpleUploadedFile(
            'teacher-profile.png',
            image_data.getvalue(),
            content_type='image/png',
        )

    def test_wrong_role_sign_in_is_rejected_for_every_role_pair(self):
        accounts = [
            ('admin', self.admin),
            ('teacher', self.teacher),
            ('learner', self._learner('role-pair@example.com', 'Role')),
        ]
        for account_role, account in accounts:
            for selected_role, _ in accounts:
                if account_role == selected_role:
                    continue
                with self.subTest(account=account_role, selected=selected_role):
                    self.client.logout()
                    response = self.client.post(reverse('user_login'), {
                        'username': account.username,
                        'password': 'valid-password',
                        'role': selected_role,
                    })
                    self.assertRedirects(
                        response,
                        reverse('role_login', args=[selected_role]),
                        fetch_redirect_response=False,
                    )
                    self.assertNotIn('_auth_user_id', self.client.session)

    def test_teacher_dashboard_and_profile_are_available(self):
        self.client.force_login(self.teacher)
        response = self.client.get(reverse('teacher_home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Total Learners')
        self.assertContains(response, 'Total Subjects')

        response = self.client.post(reverse('teacher_profile'), {
            'first_name': 'Updated',
            'last_name': 'Teacher',
            'email': 'changed@example.com',
        })
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.first_name, 'Updated')
        self.assertEqual(self.teacher.email, 'teacher-role@example.com')

    def test_teacher_can_manage_learners_and_admin_can_manage_teachers(self):
        self._set_up_class()
        self.client.force_login(self.teacher)
        response = self.client.get(reverse('manage_student'))
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            reverse('add_student'),
            {
                'first_name': 'Added',
                'last_name': 'Learner',
                'gender': 'F',
                'password': 'A-Unique-Strong-Password-289!',
                'address': 'School Road',
                'profile_pic': self._image(),
            },
        )
        self.assertRedirects(
            response, reverse('manage_student'),
            fetch_redirect_response=False)
        added_learner = CustomUser.objects.get(
            username='learnera',
            user_type='2',
        )
        self.assertFalse(added_learner.pin_hash)

        learner = self._learner('teacher-edit-learner@example.com', 'Before')
        original_account_id = learner.username
        response = self.client.post(
            reverse('edit_student', args=[learner.student.pk]),
            {
                'first_name': 'After',
                'last_name': 'Learner',
                'gender': 'F',
                'password': '',
                'profile_pic': self._image(),
                'address': 'School Road',
                'grade_and_class': '8',
                'academic_period': self.academic_period.pk,
            },
        )
        self.assertEqual(
            response.status_code, 302,
            str(response.context['form'].errors) if response.context else '',
        )
        learner.refresh_from_db()
        self.assertEqual(learner.first_name, 'After')
        self.assertEqual(learner.username, original_account_id)
        response = self.client.post(
            reverse('delete_student', args=[learner.student.pk]))
        self.assertRedirects(
            response, reverse('manage_student'),
            fetch_redirect_response=False)
        self.assertFalse(CustomUser.objects.filter(pk=learner.pk).exists())

        self.client.force_login(self.admin)
        response = self.client.post(reverse('add_teacher'), {
            'first_name': 'New',
            'last_name': 'Teacher',
            'gender': 'M',
            'password': 'A-Unique-Strong-Password-289!',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'teachern')
        managed = CustomUser.objects.get(username='teachern')
        self.assertTrue(check_password(
            response.context['issued_pin'], managed.pin_hash))
        self.assertFalse(managed.has_usable_password())
        teacher_profile = managed.teacher
        response = self.client.get(reverse('manage_teachers'))
        self.assertContains(response, managed.username)

        response = self.client.post(
            reverse('edit_teacher', args=[teacher_profile.pk]), {
                'first_name': 'Updated',
                'last_name': 'Teacher',
                'gender': 'F',
                'password': '',
            })
        self.assertRedirects(
            response, reverse('manage_teachers'),
            fetch_redirect_response=False)
        managed.refresh_from_db()
        self.assertEqual(managed.username, 'teachern')

        response = self.client.post(
            reverse('delete_teacher', args=[teacher_profile.pk]))
        self.assertRedirects(
            response, reverse('manage_teachers'),
            fetch_redirect_response=False)
        self.assertFalse(CustomUser.objects.filter(pk=managed.pk).exists())

    def test_adding_learner_does_not_require_address_or_profile_picture(self):
        self.client.force_login(self.teacher)

        response = self.client.post(reverse('add_student'), {
            'first_name': 'No',
            'last_name': 'Photo',
            'gender': 'F',
            'password': 'A-Unique-Strong-Password-289!',
        })

        self.assertRedirects(
            response, reverse('manage_student'),
            fetch_redirect_response=False)
        learner = CustomUser.objects.get(username='photon')
        self.assertEqual(learner.address, '')
        self.assertFalse(learner.profile_pic)
        self.assertFalse(learner.pin_hash)
        self.assertTrue(learner.student.teacher_requests.filter(
            teacher=self.teacher.teacher,
            status=LearnerTeacherRequest.ACCEPTED,
        ).exists())
        self.assertEqual(learner.student.created_by, self.teacher.teacher)

    def test_admin_and_teacher_pages_are_separated(self):
        self._set_up_class()
        learner = self._learner('restricted-learner@example.com', 'Private')
        result = StudentResult.objects.create(
            student=learner.student,
            subject=self.subject,
            test=20,
            exam=70,
        )
        admin_forbidden = (
            'manage_student_results',
            'generate_report_card',
        )
        self.client.force_login(self.admin)
        for page in admin_forbidden:
            with self.subTest(role='admin', page=page):
                response = self.client.get(reverse(page))
                self.assertRedirects(
                    response, reverse('admin_home'),
                    fetch_redirect_response=False)

        teacher_forbidden = (
            'manage_teachers',
            'add_teacher',
            'view_student_leave',
            'student_feedback_message',
            'admin_notify_student',
            'learner_login_activity',
            'admin_home',
        )
        self.client.force_login(self.teacher)
        for page in teacher_forbidden:
            with self.subTest(role='teacher', page=page):
                url = reverse(page)
                response = self.client.get(url)
                self.assertRedirects(
                    response, reverse('teacher_home'),
                    fetch_redirect_response=False)
        response = self.client.post(reverse('generate_report'), {
            'report_type': 'school_summary',
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.get('Content-Type'), 'application/pdf')

        self.client.force_login(learner)
        for page in (
                'teacher_home', 'manage_student',
                'manage_student_results', 'manage_teachers', 'admin_home'):
            with self.subTest(role='learner', page=page):
                response = self.client.get(reverse(page))
                self.assertRedirects(
                    response, reverse('student_home'),
                    fetch_redirect_response=False)
        self.assertTrue(result.pk)

    def test_administrator_assignments_are_read_only_and_teacher_sets_up_subjects(self):
        self._set_up_class()
        learner = CustomUser.objects.create_user(
            username='unassigned-learner',
            password='valid-password',
            user_type='2',
            gender='F',
        )
        subject = Subject.objects.create(name='Unassigned Subject')
        self.client.force_login(self.admin)

        response = self.client.get(reverse('manage_assignments'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Unassigned Subject')
        self.assertContains(response, 'Not set up')
        self.assertNotContains(response, 'Assign learners')
        self.assertNotContains(response, 'Save learner assignments')
        self.assertEqual(
            self.client.post(reverse('manage_assignments')).status_code,
            405,
        )
        learner.student.refresh_from_db()
        self.assertFalse(learner.student.teacher_requests.exists())
        self.assertIsNone(learner.student.created_by)

        self.assertEqual(self.client.get('/subject/add/').status_code, 404)
        subject.refresh_from_db()
        self.assertIsNone(subject.teacher)
        self.assertIsNone(subject.created_by)

        self.client.force_login(self.teacher)
        response = self.client.post(reverse('teacher_classes_subjects'), {
            'action': 'add_subject',
            'name': 'Teacher Created Subject',
            'school_class': self.school_class.pk,
        })
        self.assertRedirects(
            response, reverse('teacher_classes_subjects'),
            fetch_redirect_response=False)
        created_subject = Subject.objects.get(name='Teacher Created Subject')
        self.assertEqual(created_subject.teacher, self.teacher.teacher)
        self.assertEqual(created_subject.created_by, self.teacher.teacher)

    def test_teacher_cannot_access_another_teachers_learner_or_records(self):
        self._set_up_class()
        other_teacher = CustomUser.objects.create_user(
            username='other-teacher',
            password='valid-password',
            user_type='3',
            gender='M',
        )
        learner = CustomUser.objects.create_user(
            username='other-assigned-learner',
            password='valid-password',
            user_type='2',
            gender='F',
        )
        learner.student.school_class = self.school_class
        learner.student.academic_period = self.academic_period
        learner.student.save()
        accept_learner_for_teacher(learner.student, other_teacher.teacher)
        subject = Subject.objects.create(
            name='Other Teacher Subject',
            school_class=self.school_class,
            teacher=other_teacher.teacher,
        )
        result = StudentResult.objects.create(
            student=learner.student, subject=subject, test=10, exam=20)
        self.client.force_login(self.teacher)

        response = self.client.get(reverse('manage_student'))
        self.assertNotContains(response, learner.username)
        self.assertEqual(response.status_code, 200)
        for url_name, args in (
            ('edit_student', [learner.student.pk]),
            ('edit_student_result', [result.pk]),
            ('delete_student_result', [result.pk]),
        ):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name, args=args))
                self.assertEqual(response.status_code, 404)
        self.assertEqual(
            self.client.get(f'/subject/edit/{subject.pk}').status_code, 404)
        self.assertEqual(
            self.client.get(f'/subject/delete/{subject.pk}').status_code, 404)

        response = self.client.post(reverse('generate_report_card'), {
            'student': learner.student.pk,
            'academic_period': self.academic_period.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.get('Content-Type'), 'application/pdf')

    def test_deleting_teacher_preserves_learners_subjects_and_academic_records(self):
        self._set_up_class()
        learner = self._learner('preserved-learner@example.com', 'Preserved')
        learner.student.created_by = self.teacher.teacher
        learner.student.save()
        result = StudentResult.objects.create(
            student=learner.student,
            subject=self.subject,
            created_by=self.teacher.teacher,
            test=20,
            exam=70,
        )
        self.teacher.delete()

        learner.student.refresh_from_db()
        self.subject.refresh_from_db()
        result.refresh_from_db()
        self.assertFalse(learner.student.teacher_requests.exists())
        self.assertIsNone(learner.student.created_by)
        self.assertTrue(Student.objects.filter(pk=learner.student.pk).exists())
        self.assertIsNone(self.subject.teacher)
        self.assertIsNone(self.subject.created_by)
        self.assertTrue(Subject.objects.filter(pk=self.subject.pk).exists())
        self.assertIsNone(result.created_by)

    def test_class_order_is_numeric(self):
        labels = [f'Grade {number}' for number in range(1, 11)]
        for number in reversed(range(1, 11)):
            SchoolClass.objects.create(grade_number=number)

        from .models import ordered_school_classes

        self.assertEqual(
            [str(item) for item in ordered_school_classes()],
            labels,
        )

    @staticmethod
    def _profile_image_upload():
        image_data = BytesIO()
        Image.new('RGB', (1, 1), color='white').save(image_data, format='PNG')
        return SimpleUploadedFile(
            'profile.png', image_data.getvalue(), content_type='image/png')

    def test_admin_can_download_pdf_reports(self):
        admin = CustomUser.objects.create_user(
            email='report-admin@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
            first_name='Report',
            last_name='Admin',
        )
        learner = CustomUser.objects.create_user(
            email='report-learner@example.com',
            username='report-learner',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
            first_name='Test',
            last_name='Learner',
        )
        school_class = SchoolClass.objects.create(grade_number=7)
        academic_period = AcademicPeriod.objects.create(
            start_year='2026-01-01', end_year='2026-12-31')
        subject = Subject.objects.create(
            name='Mathematics', school_class=school_class)
        learner.student.school_class = school_class
        learner.student.academic_period = academic_period
        learner.student.save()
        StudentResult.objects.create(
            student=learner.student, subject=subject, test=18, exam=72)
        self.client.force_login(admin)

        report_options = [
            {'report_type': 'school_summary'},
            {
                'report_type': 'class_list',
                'school_class': school_class.pk,
                'academic_period': academic_period.pk,
            },
        ]
        for options in report_options:
            with self.subTest(report_type=options['report_type']):
                response = self.client.post(reverse('generate_report'), options)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Content-Type'], 'application/pdf')
                self.assertTrue(response.content.startswith(b'%PDF'))
                self.assertIn(b'/Subtype /Image', response.content)
                self.assertIn('attachment;', response['Content-Disposition'])

        from .hod_views import _report_data

        _, headers, rows = _report_data(
            'class_list', school_class, academic_period, None)
        self.assertEqual(
            headers, ['No.', 'Learner name', 'Gender', 'Account ID'])
        self.assertEqual(rows[0][3], learner.username)

    def test_teacher_can_download_results_report(self):
        teacher = CustomUser.objects.create_user(
            email='report-teacher@example.com',
            password='valid-password',
            user_type='3',
            gender='F',
            address='',
        )
        learner = CustomUser.objects.create_user(
            email='report-result-learner@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        school_class = SchoolClass.objects.create(grade_number=7)
        TeacherClassSetup.objects.create(
            teacher=teacher.teacher, school_class=school_class)
        academic_period = AcademicPeriod.objects.create(
            start_year='2026-01-01', end_year='2026-12-31')
        subject = Subject.objects.create(
            name='Mathematics',
            school_class=school_class,
            teacher=teacher.teacher,
        )
        learner.student.school_class = school_class
        learner.student.academic_period = academic_period
        learner.student.save()
        accept_learner_for_teacher(learner.student, teacher.teacher)
        StudentResult.objects.create(
            student=learner.student, subject=subject, test=18, exam=72)
        self.client.force_login(teacher)

        response = self.client.post(reverse('generate_report'), {
            'report_type': 'learner_results',
            'school_class': school_class.pk,
            'academic_period': academic_period.pk,
            'subject': subject.pk,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn(b'/Subtype /Image', response.content)

    def test_teacher_can_add_and_update_result_and_learner_sees_latest_scores(self):
        teacher = CustomUser.objects.create_user(
            email='result-manager@example.com',
            password='valid-password',
            user_type='3',
            gender='F',
            address='',
        )
        learner = CustomUser.objects.create_user(
            email='result-owner@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
            first_name='Alex',
            last_name='Learner',
        )
        school_class = SchoolClass.objects.create(grade_number=7)
        TeacherClassSetup.objects.create(
            teacher=teacher.teacher, school_class=school_class)
        learner.student.school_class = school_class
        academic_period = AcademicPeriod.objects.create(
            start_year='2026-01-01', end_year='2026-12-31')
        learner.student.academic_period = academic_period
        learner.student.save()
        accept_learner_for_teacher(learner.student, teacher.teacher)
        subject = Subject.objects.create(
            name='Mathematics',
            school_class=school_class,
            teacher=teacher.teacher,
        )
        self.client.force_login(teacher)

        response = self.client.post(reverse('manage_student_results'), {
            'action': 'save',
            'school_class': school_class.pk,
            'subject': subject.pk,
            'academic_period': academic_period.pk,
            'assessment_ids': ['', ''],
            'assessment_0_name': 'Test 1',
            'assessment_0_weight': '50',
            'assessment_0_maximum': '100',
            'assessment_1_name': 'Examination',
            'assessment_1_weight': '50',
            'assessment_1_maximum': '100',
            f'score_{learner.student.pk}_0': '18',
            f'score_{learner.student.pk}_1': '72',
        })

        self.assertRedirects(
            response,
            reverse('manage_student_results') +
            f'?school_class={school_class.pk}&subject={subject.pk}'
            f'&academic_period={academic_period.pk}',
            fetch_redirect_response=False,
        )
        result = StudentResult.objects.get(
            student=learner.student, subject=subject)
        self.assertEqual(len(result.assessment_scores), 2)
        self.assertEqual(result.created_by, teacher.teacher)
        assessment_ids = list(result.assessment_scores)

        response = self.client.post(reverse('manage_student_results'), {
            'action': 'save',
            'school_class': school_class.pk,
            'subject': subject.pk,
            'academic_period': academic_period.pk,
            'assessment_ids': assessment_ids,
            'assessment_0_name': 'Test 1',
            'assessment_0_weight': '50',
            'assessment_0_maximum': '100',
            'assessment_1_name': 'Examination',
            'assessment_1_weight': '50',
            'assessment_1_maximum': '100',
            f'score_{learner.student.pk}_0': '20',
            f'score_{learner.student.pk}_1': '75',
        })
        self.assertRedirects(
            response,
            reverse('manage_student_results') +
            f'?school_class={school_class.pk}&subject={subject.pk}'
            f'&academic_period={academic_period.pk}',
            fetch_redirect_response=False,
        )

        self.client.force_login(learner)
        response = self.client.get(reverse('student_view_result'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Mathematics')
        self.assertRegex(response.content.decode(), r'20\s*/ 100\.00')
        self.assertRegex(response.content.decode(), r'75\s*/ 100\.00')
        self.assertContains(response, '47.50')

    def test_learner_cannot_access_admin_result_management(self):
        learner = CustomUser.objects.create_user(
            email='result-access-denied@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        self.client.force_login(learner)

        response = self.client.get(reverse('manage_student_results'))

        self.assertRedirects(
            response,
            reverse('student_home'),
            fetch_redirect_response=False,
        )

    def test_learner_is_redirected_from_report_page(self):
        learner = CustomUser.objects.create_user(
            email='report-denied@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        self.client.force_login(learner)

        response = self.client.get(reverse('generate_report'))

        self.assertRedirects(
            response,
            reverse('student_home'),
            fetch_redirect_response=False,
        )

    def test_empty_report_shows_message_instead_of_returning_pdf(self):
        admin = CustomUser.objects.create_user(
            email='empty-report-admin@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        self.client.force_login(admin)

        response = self.client.post(reverse('generate_report'), {
            'report_type': 'school_summary',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/html; charset=utf-8')
        self.assertContains(
            response, 'There is no data for the selected report options.')

    def test_admin_can_sign_in_through_admin_interface(self):
        admin = CustomUser.objects.create_user(
            username='admin-login-interface',
            password='valid-password',
            user_type=1,
            gender='F',
            address='',
        )
        admin_pin = issue_pin(admin)

        response = self.client.post(reverse('user_login'), {
            'username': admin.username,
            'pin': admin_pin,
            'role': 'admin',
        })

        self.assertRedirects(
            response,
            reverse('admin_home'),
            fetch_redirect_response=False,
        )

    def test_admin_cannot_sign_in_through_learner_interface(self):
        CustomUser.objects.create_user(
            email='admin@example.com',
            password='valid-password',
            user_type=1,
            gender='F',
            address='',
        )

        response = self.client.post(reverse('user_login'), {
            'username': 'admin@example.com',
            'password': 'valid-password',
            'role': 'learner',
        })

        self.assertRedirects(
            response,
            reverse('role_login', args=['learner']),
            fetch_redirect_response=False,
        )
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_wrong_password_does_not_sign_in(self):
        CustomUser.objects.create_user(
            email='learner@example.com',
            password='valid-password',
            user_type=2,
            gender='F',
            address='',
        )

        response = self.client.post(reverse('user_login'), {
            'username': 'learner@example.com',
            'password': 'incorrect-password',
            'role': 'learner',
        })

        self.assertRedirects(
            response,
            reverse('role_login', args=['learner']),
            fetch_redirect_response=False,
        )
        self.assertNotIn('_auth_user_id', self.client.session)


class TeacherLearnerRequestTests(TestCase):
    def setUp(self):
        self.teacher = CustomUser.objects.create_user(
            username='request-teacher',
            password='valid-password',
            user_type='3',
            gender='F',
            first_name='Taylor',
            last_name='Teacher',
        )
        self.class_a = SchoolClass.objects.create(
            grade_number=8, class_letter='a')
        self.class_b = SchoolClass.objects.create(
            grade_number=8, class_letter='b')

    def _learner(self, username, school_class):
        learner = CustomUser.objects.create_user(
            username=username,
            password='valid-password',
            user_type='2',
            gender='F',
            first_name=username.title(),
            last_name='Learner',
        )
        learner.student.school_class = school_class
        learner.student.save(update_fields=['school_class', 'updated_at'])
        return learner

    def test_registration_requests_only_the_exact_class_setups(self):
        grade_only_teacher = CustomUser.objects.create_user(
            username='grade-only-teacher',
            password='valid-password',
            user_type='3',
            gender='M',
        )
        TeacherClassSetup.objects.create(
            teacher=grade_only_teacher.teacher,
            school_class=SchoolClass.objects.create(grade_number=8),
        )
        TeacherClassSetup.objects.create(
            teacher=self.teacher.teacher,
            school_class=self.class_a,
        )

        response = self.client.post(reverse('register_learner'), {
            'first_name': 'Exact',
            'last_name': 'Class',
            'grade_and_class': '8A',
            'gender': 'F',
            'password': 'A-Unique-Strong-Password-289!',
            'confirm_password': 'A-Unique-Strong-Password-289!',
        }, follow=True)

        learner = CustomUser.objects.get(username='classe')
        self.assertEqual(
            response.redirect_chain[0][0],
            reverse('role_login', args=['learner']),
        )
        requests = list(learner.student.teacher_requests.all())
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].teacher, self.teacher.teacher)
        self.assertEqual(requests[0].school_class, self.class_a)
        self.assertEqual(requests[0].status, LearnerTeacherRequest.PENDING)
        self.assertNotContains(response, 'Waiting for a teacher')
        self.assertNotContains(response, 'Pending')

    def test_adding_a_class_queues_requests_for_existing_learners(self):
        learner = self._learner('preexisting-learner', self.class_a)
        self.client.force_login(self.teacher)

        response = self.client.post(
            reverse('teacher_classes_subjects'), {
                'action': 'add_class',
                'grade_and_class': '8A',
            })

        self.assertRedirects(
            response, reverse('teacher_classes_subjects'),
            fetch_redirect_response=False)
        learner_request = learner.student.teacher_requests.get()
        self.assertEqual(learner_request.teacher, self.teacher.teacher)
        self.assertEqual(learner_request.status, LearnerTeacherRequest.PENDING)
        self.assertTrue(TeacherClassSetup.objects.filter(
            teacher=self.teacher.teacher,
            school_class=self.class_a,
        ).exists())
        self.assertTrue(ActivityLog.objects.filter(
            object_type='main_app.teacherclasssetup',
            action='create',
        ).exists())

    def test_filtered_accept_all_and_decline_all_apply_only_to_shown_requests(self):
        TeacherClassSetup.objects.create(
            teacher=self.teacher.teacher, school_class=self.class_a)
        TeacherClassSetup.objects.create(
            teacher=self.teacher.teacher, school_class=self.class_b)
        learner_a = self._learner('request-learner-a', self.class_a)
        learner_b = self._learner('request-learner-b', self.class_b)
        create_class_requests(learner_a.student)
        create_class_requests(learner_b.student)
        request_a = learner_a.student.teacher_requests.get()
        request_b = learner_b.student.teacher_requests.get()
        self.client.force_login(self.teacher)

        response = self.client.get(
            reverse('learner_requests'), {'school_class': self.class_a.pk})
        self.assertContains(response, learner_a.get_full_name())
        self.assertNotContains(response, learner_b.username)
        self.assertContains(response, 'This action will affect 1 pending learner')

        response = self.client.post(reverse('learner_requests'), {
            'school_class': self.class_a.pk,
            'action': 'accept_all',
        })
        self.assertRedirects(
            response,
            f"{reverse('learner_requests')}?school_class={self.class_a.pk}",
            fetch_redirect_response=False,
        )
        request_a.refresh_from_db()
        request_b.refresh_from_db()
        self.assertEqual(request_a.status, LearnerTeacherRequest.ACCEPTED)
        self.assertEqual(request_b.status, LearnerTeacherRequest.PENDING)

        response = self.client.post(reverse('learner_requests'), {
            'school_class': self.class_b.pk,
            'action': 'decline_all',
        })
        self.assertRedirects(
            response,
            f"{reverse('learner_requests')}?school_class={self.class_b.pk}",
            fetch_redirect_response=False,
        )
        request_b.refresh_from_db()
        self.assertEqual(request_b.status, LearnerTeacherRequest.DECLINED)

        self.client.force_login(learner_a)
        self.assertContains(
            self.client.get(reverse('student_home')),
            'Accepted by Taylor Teacher',
        )
        self.client.force_login(learner_b)
        self.assertContains(
            self.client.get(reverse('student_home')),
            'Not accepted',
        )


class AssessmentResultsTests(TestCase):
    def setUp(self):
        self.teacher = CustomUser.objects.create_user(
            username='assessment-teacher', password='valid-password',
            user_type='3', gender='F')
        self.school_class = SchoolClass.objects.create(grade_number=8)
        TeacherClassSetup.objects.create(
            teacher=self.teacher.teacher, school_class=self.school_class)
        self.period = AcademicPeriod.objects.create(
            year=2026, term=1, is_current=True)
        self.subject = Subject.objects.create(
            name='Mathematics', school_class=self.school_class,
            teacher=self.teacher.teacher)
        self.client.force_login(self.teacher)

    def _learner(self, username, accepted=True):
        user = CustomUser.objects.create_user(
            username=username, password='valid-password', user_type='2',
            gender='F', first_name=username, last_name='Learner')
        user.student.school_class = self.school_class
        user.student.academic_period = self.period
        user.student.save()
        if accepted:
            accept_learner_for_teacher(user.student, self.teacher.teacher)
        return user

    def _get_grid(self, subject=None, school_class=None, period=None):
        return self.client.get(reverse('manage_student_results'), {
            'school_class': (school_class or self.school_class).pk,
            'subject': (subject or self.subject).pk,
            'academic_period': (period or self.period).pk,
        })

    @staticmethod
    def _payload(rows, learner_marks=None, action='save', remove_id=None,
                 school_class=None, subject=None, period=None):
        data = {
            'school_class': school_class.pk,
            'subject': subject.pk,
            'academic_period': period.pk,
            'assessment_ids': [str(row.get('pk') or '') for row in rows],
        }
        if action:
            data['action'] = action
        if remove_id:
            data['remove_assessment'] = str(remove_id)
        for index, row in enumerate(rows):
            data[f'assessment_{index}_name'] = row['name']
            data[f'assessment_{index}_weight'] = str(row['weight'])
            data[f'assessment_{index}_maximum'] = str(row['maximum_mark'])
        for learner_id, marks in (learner_marks or {}).items():
            for index, mark in enumerate(marks):
                if mark is not None:
                    data[f'score_{learner_id}_{index}'] = str(mark)
        return data

    def test_default_grid_has_two_assessments_and_only_accepted_learners(self):
        accepted = self._learner('accepted-learner')
        self._learner('pending-learner', accepted=False)

        response = self._get_grid()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Test 1')
        self.assertContains(response, 'Examination')
        self.assertContains(response, accepted.username)
        self.assertNotContains(response, 'pending-learner')
        self.assertEqual(
            [row['name'] for row in response.context['assessment_rows']],
            ['Test 1', 'Examination'],
        )

    def test_saving_marks_creates_result_and_calculates_final_grade(self):
        learner = self._learner('marks-learner')
        rows = [
            {'pk': '', 'name': 'Test 1', 'weight': 50, 'maximum_mark': 100},
            {'pk': '', 'name': 'Examination', 'weight': 50, 'maximum_mark': 100},
        ]

        response = self.client.post(reverse('manage_student_results'), self._payload(
            rows, {learner.student.pk: [80, 100]},
            school_class=self.school_class, subject=self.subject,
            period=self.period))

        self.assertRedirects(
            response,
            reverse('manage_student_results') +
            f'?school_class={self.school_class.pk}&subject={self.subject.pk}'
            f'&academic_period={self.period.pk}',
            fetch_redirect_response=False)
        result = StudentResult.objects.get(student=learner.student)
        self.assertEqual(result.academic_period, self.period)
        self.assertEqual(result.created_by, self.teacher.teacher)
        self.assertTrue(ActivityLog.objects.filter(
            actor=self.teacher,
            object_type='main_app.assessment',
            action='create',
        ).exists())
        self.assertTrue(ActivityLog.objects.filter(
            actor=self.teacher,
            object_type='main_app.studentresult',
            action='create',
        ).exists())
        from .grading import prepare_result
        prepare_result(result)
        self.assertEqual(result.total_score, Decimal('90.00'))
        self.assertEqual(result.grade['symbol'], 'A*')

    def test_adding_and_removing_an_assessment_deletes_its_saved_marks(self):
        learner = self._learner('test-column-learner')
        grid = self._get_grid()
        rows = grid.context['assessment_rows']
        response = self.client.post(reverse('manage_student_results'), self._payload(
            rows, action='add_test', school_class=self.school_class,
            subject=self.subject, period=self.period))
        self.assertEqual(response.status_code, 302)

        plan = AssessmentPlan.objects.get(
            school_class=self.school_class, subject=self.subject,
            academic_period=self.period)
        assessments = list(plan.assessments.filter(
            is_previous_record=False).order_by('position'))
        self.assertEqual([item.name for item in assessments], [
            'Test 1', 'Test 2', 'Examination'])
        learner_result = StudentResult.objects.create(
            student=learner.student, subject=self.subject,
            academic_period=None,
            assessment_scores={str(assessments[1].pk): '25'})

        grid = self._get_grid()
        rows = grid.context['assessment_rows']
        response = self.client.post(reverse('manage_student_results'), self._payload(
            rows, {learner.student.pk: [None, 25, None]}, action=None,
            remove_id=assessments[1].pk, school_class=self.school_class,
            subject=self.subject, period=self.period))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Assessment.objects.filter(pk=assessments[1].pk).exists())
        learner_result.refresh_from_db()
        self.assertNotIn(str(assessments[1].pk), learner_result.assessment_scores)

    def test_invalid_marks_are_rejected_and_incomplete_marks_stay_blank(self):
        learner = self._learner('incomplete-learner')
        grid = self._get_grid()
        rows = grid.context['assessment_rows']
        bad_marks = {learner.student.pk: [101, 80]}

        response = self.client.post(reverse('manage_student_results'), self._payload(
            rows, bad_marks, school_class=self.school_class,
            subject=self.subject, period=self.period))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'marks must be between 0 and 100')
        self.assertFalse(StudentResult.objects.exists())

        invalid_rows = [
            {'pk': '', 'name': 'Test 1', 'weight': 'NaN', 'maximum_mark': 100},
            {'pk': '', 'name': 'Examination', 'weight': 50, 'maximum_mark': 100},
        ]
        response = self.client.post(reverse('manage_student_results'), self._payload(
            invalid_rows, school_class=self.school_class,
            subject=self.subject, period=self.period))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'weight must be between 0 and 100%')
        self.assertFalse(AssessmentPlan.objects.exists())

        response = self.client.post(reverse('manage_student_results'), self._payload(
            rows, {learner.student.pk: [80, None]},
            school_class=self.school_class, subject=self.subject,
            period=self.period))
        self.assertEqual(response.status_code, 302)
        result = StudentResult.objects.get(student=learner.student)
        self.assertEqual(len(result.assessment_scores), 1)
        from .grading import prepare_result
        prepare_result(result)
        self.assertIsNone(result.total_score)

    def test_weights_warning_and_no_final_until_total_is_100(self):
        learner = self._learner('weight-warning-learner')
        rows = [
            {'pk': '', 'name': 'Test 1', 'weight': 60, 'maximum_mark': 100},
            {'pk': '', 'name': 'Examination', 'weight': 39, 'maximum_mark': 100},
        ]
        response = self.client.post(reverse('manage_student_results'), self._payload(
            rows, {learner.student.pk: [90, 90]},
            school_class=self.school_class, subject=self.subject,
            period=self.period))
        self.assertEqual(response.status_code, 302)
        result = StudentResult.objects.get(student=learner.student)
        from .grading import prepare_result
        prepare_result(result)
        self.assertIsNone(result.total_score)

        response = self._get_grid()
        self.assertContains(response, 'Weights must total 100%')
        self.assertContains(response, '99.00%')

    def test_previous_period_settings_are_copied_without_marks(self):
        previous_plan = AssessmentPlan.objects.create(
            school_class=self.school_class, subject=self.subject,
            academic_period=self.period)
        Assessment.objects.create(
            plan=previous_plan, name='Quiz', weight=25, maximum_mark=20,
            position=0)
        Assessment.objects.create(
            plan=previous_plan, name='Final', weight=75, maximum_mark=100,
            position=1)
        current_period = AcademicPeriod.objects.create(year=2026, term=2)

        response = self._get_grid(period=current_period)

        self.assertEqual(
            [(row['name'], str(row['weight']), str(row['maximum_mark']))
             for row in response.context['assessment_rows']],
            [('Quiz', '25.00', '20.00'), ('Final', '75.00', '100.00')],
        )
        self.assertEqual(response.context['assessment_rows'][0]['pk'], '')

    def test_teacher_cannot_manage_another_teachers_subject_or_class(self):
        other_teacher = CustomUser.objects.create_user(
            username='other-assessment-teacher', password='valid-password',
            user_type='3', gender='F')
        other_class = SchoolClass.objects.create(grade_number=9)
        other_subject = Subject.objects.create(
            name='Science', school_class=other_class,
            teacher=other_teacher.teacher)

        response = self._get_grid(subject=other_subject, school_class=other_class)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['selected_subject'])
        self.assertFalse(AssessmentPlan.objects.exists())

    def test_fixed_grading_scale_includes_exact_89_99_and_90_boundaries(self):
        from .grading import get_grade

        expected = (
            ('100.00', 'A*'),
            ('90.00', 'A*'),
            ('89.99', 'A'),
            ('80.00', 'A'),
            ('79.99', 'B'),
            ('70.00', 'B'),
            ('69.99', 'C'),
            ('60.00', 'C'),
            ('59.99', 'D'),
            ('50.00', 'D'),
            ('49.99', 'E'),
            ('40.00', 'E'),
            ('39.99', 'F'),
            ('30.00', 'F'),
            ('29.99', 'G'),
            ('20.00', 'G'),
            ('19.99', 'U'),
            ('0.00', 'U'),
        )
        for mark, symbol in expected:
            with self.subTest(mark=mark):
                self.assertEqual(get_grade(mark)['symbol'], symbol)
        self.assertEqual(get_grade('89.995')['symbol'], 'A*')
        self.assertIsNone(get_grade('-0.01'))
        self.assertIsNone(get_grade('100.01'))

    def test_legacy_scores_remain_hidden_on_previous_records(self):
        from .grading import prepare_result

        learner = self._learner('previous-record-learner')
        plan = AssessmentPlan.objects.create(
            school_class=self.school_class, subject=self.subject,
            academic_period=self.period)
        previous = Assessment.objects.create(
            plan=plan, name='Previous total (test + exam)', weight=100,
            maximum_mark=100, position=9999, is_previous_record=True)
        Assessment.objects.create(
            plan=plan, name='Test 1', weight=50, maximum_mark=100)
        Assessment.objects.create(
            plan=plan, name='Examination', weight=50, maximum_mark=100)
        result = StudentResult.objects.create(
            student=learner.student, subject=self.subject,
            academic_period=self.period, test_1=40, exam=60,
            assessment_scores={str(previous.pk): '50.00'},
            is_previous_record=True)

        prepare_result(result)

        self.assertEqual(result.total_score, Decimal('50.00'))
        self.assertEqual(result.display_assessments, [previous])
        self.assertEqual((result.test_1, result.exam), (40, 60))

    def test_report_card_exports_use_dynamic_assessments_and_fixed_grades(self):
        learner = self._learner('report-grid-learner')
        plan = AssessmentPlan.objects.create(
            school_class=self.school_class, subject=self.subject,
            academic_period=self.period)
        test = Assessment.objects.create(
            plan=plan, name='Quiz', weight=50, maximum_mark=20, position=0)
        exam = Assessment.objects.create(
            plan=plan, name='Final', weight=50, maximum_mark=100, position=1)
        StudentResult.objects.create(
            student=learner.student, subject=self.subject,
            academic_period=self.period,
            assessment_scores={str(test.pk): '18', str(exam.pk): '72'})

        response = self.client.post(reverse('generate_report_card'), {
            'student': learner.student.pk,
            'academic_period': self.period.pk,
            'export_format': 'csv',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn(
            b'END OF TERM 1 2026 LEARNER REPORT CARD', response.content)
        self.assertIn(
            b'C. Heuva Junior Secondary School', response.content)
        self.assertIn(b'Generated:', response.content)
        self.assertIn(b'Quiz (50.00%)', response.content)
        self.assertIn(b'Final (50.00%)', response.content)
        self.assertIn(b',18,72,', response.content)
        self.assertIn(b'A', response.content)
        self.assertIn(b'US A+', response.content)

        with patch(
                'main_app.hod_views._create_report_pdf',
                return_value=HttpResponse(b'%PDF-test')) as pdf_builder:
            pdf_response = self.client.post(reverse('generate_report_card'), {
                'student': learner.student.pk,
                'academic_period': self.period.pk,
                'export_format': 'pdf',
            })
        self.assertEqual(pdf_response.content, b'%PDF-test')
        self.assertEqual(
            pdf_builder.call_args.kwargs['report_card']['student'],
            learner.student,
        )
        self.assertEqual(
            pdf_builder.call_args.args[1],
            'END OF TERM 1 2026 LEARNER REPORT CARD',
        )
        self.assertIn('Teacher\'s initials', pdf_builder.call_args.args[2])

    def test_results_mark_sheet_has_assessment_headers_and_class_statistics(self):
        learner = self._learner('mark-sheet-learner')
        plan = AssessmentPlan.objects.create(
            school_class=self.school_class,
            subject=self.subject,
            academic_period=self.period,
        )
        test = Assessment.objects.create(
            plan=plan, name='Quiz', weight=20, maximum_mark=20, position=0)
        exam = Assessment.objects.create(
            plan=plan, name='Examination', weight=80, maximum_mark=100,
            position=1)
        StudentResult.objects.create(
            student=learner.student,
            subject=self.subject,
            academic_period=self.period,
            assessment_scores={str(test.pk): '18', str(exam.pk): '80'},
        )

        from .hod_views import _report_data

        title, headers, rows = _report_data(
            'learner_results',
            self.school_class,
            self.period,
            self.subject,
            user=self.teacher,
        )

        self.assertIn('Results Mark Sheet', title)
        self.assertEqual(headers, [
            'Learner name', 'Quiz (20.00%)', 'Examination (80.00%)',
            'Final %', 'Grade',
        ])
        self.assertEqual(rows[0][0], 'mark-sheet-learner Learner')
        self.assertEqual(rows[0][3:], ['82.00', 'A'])
        self.assertTrue(any(row[0] == 'Pass rate (50% and above)' for row in rows))
        self.assertTrue(any(row[0] == 'Learners with grade A' for row in rows))

    def test_results_mark_sheet_includes_accepted_learners_without_marks(self):
        accepted = self._learner('mark-sheet-incomplete-learner')
        self._learner('mark-sheet-pending-learner', accepted=False)

        from .hod_views import _report_data

        _title, headers, rows = _report_data(
            'learner_results',
            self.school_class,
            self.period,
            self.subject,
            user=self.teacher,
        )

        self.assertEqual(headers, [
            'Learner name', 'Test 1 (50%)', 'Examination (50%)',
            'Final %', 'Grade',
        ])
        self.assertEqual(rows[0], [
            f'{accepted.first_name} {accepted.last_name}',
            '', '', 'Incomplete', '-',
        ])
        self.assertNotIn(
            'mark-sheet-pending-learner Learner',
            [row[0] for row in rows],
        )

    def test_teacher_report_card_is_pdf_and_respects_subject_ownership(self):
        learner = self._learner('teacher-card-learner')
        other_teacher = CustomUser.objects.create_user(
            username='report-card-other-teacher',
            password='valid-password', user_type='3', gender='F')
        own_plan = AssessmentPlan.objects.create(
            school_class=self.school_class, subject=self.subject,
            academic_period=self.period)
        own_assessment = Assessment.objects.create(
            plan=own_plan, name='Examination', weight=100, maximum_mark=100)
        own_result = StudentResult.objects.create(
            student=learner.student, subject=self.subject,
            academic_period=self.period,
            assessment_scores={str(own_assessment.pk): '80'})
        other_subject = Subject.objects.create(
            name='Science', school_class=self.school_class,
            teacher=other_teacher.teacher)
        other_plan = AssessmentPlan.objects.create(
            school_class=self.school_class, subject=other_subject,
            academic_period=self.period)
        other_assessment = Assessment.objects.create(
            plan=other_plan, name='Examination', weight=100, maximum_mark=100)
        StudentResult.objects.create(
            student=learner.student, subject=other_subject,
            academic_period=self.period,
            assessment_scores={str(other_assessment.pk): '90'})

        response = self.client.post(reverse('generate_report_card'), {
            'student': learner.student.pk,
            'academic_period': self.period.pk,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response.content.startswith(b'%PDF'))
        self.assertTrue(own_result.assessment_scores)
        self.assertNotIn(other_subject.name.encode(), response.content)

    def test_teacher_report_card_without_results_shows_message(self):
        learner = self._learner('no-report-result')

        response = self.client.post(reverse('generate_report_card'), {
            'student': learner.student.pk,
            'academic_period': self.period.pk,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/html; charset=utf-8')
        self.assertContains(
            response,
            'There are no results for this learner and academic period.',
        )

    def test_teacher_can_publish_a_report_card_only_to_its_learner(self):
        learner = self._learner('published-card-learner')
        other_learner = self._learner('other-published-card-learner')
        plan = AssessmentPlan.objects.create(
            school_class=self.school_class, subject=self.subject,
            academic_period=self.period)
        assessment = Assessment.objects.create(
            plan=plan, name='Examination', weight=100, maximum_mark=100)
        StudentResult.objects.create(
            student=learner.student,
            subject=self.subject,
            academic_period=self.period,
            assessment_scores={str(assessment.pk): '80'},
        )

        response = self.client.post(reverse('generate_report_card'), {
            'student': learner.student.pk,
            'academic_period': self.period.pk,
            'action': 'publish',
        })

        self.assertRedirects(
            response, reverse('generate_report_card'),
            fetch_redirect_response=False)
        publication = ReportCardPublication.objects.get(
            teacher=self.teacher.teacher,
            student=learner.student,
            academic_period=self.period,
        )

        self.client.force_login(learner)
        results_page = self.client.get(reverse('student_view_result'))
        self.assertContains(results_page, 'Report cards published by your teachers')
        self.assertContains(
            results_page,
            reverse('student_published_report_card', args=[publication.pk]),
        )
        pdf = self.client.get(
            reverse('student_published_report_card', args=[publication.pk]),
            {'export': 'pdf'},
        )
        self.assertEqual(pdf['Content-Type'], 'application/pdf')
        self.assertTrue(pdf.content.startswith(b'%PDF'))
        csv_response = self.client.get(
            reverse('student_published_report_card', args=[publication.pk]),
            {'export': 'csv'},
        )
        self.assertEqual(csv_response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn(b'Mathematics', csv_response.content)
        self.assertTrue(ActivityLog.objects.filter(
            actor=learner,
            action='export',
            object_type='data_export',
        ).exists())

        self.client.force_login(other_learner)
        denied = self.client.get(
            reverse('student_published_report_card', args=[publication.pk]))
        self.assertEqual(denied.status_code, 404)

    def test_teacher_cannot_publish_for_a_learner_not_accepted_by_them(self):
        learner = self._learner('unaccepted-report-card', accepted=False)
        response = self.client.post(reverse('generate_report_card'), {
            'student': learner.student.pk,
            'academic_period': self.period.pk,
            'action': 'publish',
        })

        self.assertEqual(response.status_code, 200)
        self.assertFalse(ReportCardPublication.objects.exists())

    def test_learner_report_card_contains_only_their_own_results(self):
        learner = self._learner('own-report-card')
        other_learner = self._learner('other-report-card')
        plan = AssessmentPlan.objects.create(
            school_class=self.school_class, subject=self.subject,
            academic_period=self.period)
        assessment = Assessment.objects.create(
            plan=plan, name='Examination', weight=100, maximum_mark=100)
        StudentResult.objects.create(
            student=learner.student, subject=self.subject,
            academic_period=self.period,
            assessment_scores={str(assessment.pk): '80'})
        StudentResult.objects.create(
            student=other_learner.student, subject=self.subject,
            academic_period=self.period,
            assessment_scores={str(assessment.pk): '90'})
        self.client.force_login(learner)

        with patch(
                'main_app.hod_views._create_report_pdf',
                return_value=HttpResponse(b'%PDF-own-only')) as pdf_builder:
            response = self.client.get(
                reverse('student_report_card'),
                {'student': other_learner.student.pk},
            )

        self.assertEqual(response.content, b'%PDF-own-only')
        rows = pdf_builder.call_args.args[3]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 'Mathematics')

    def test_removed_grade_band_and_assessment_weight_urls_return_404(self):
        for path in ('/admin/grade-bands/',):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        with self.assertRaises(NoReverseMatch):
            reverse('manage_assessment_weights')

class RegistrationAndRestrictedProfileTests(TestCase):
    def setUp(self):
        cache.clear()

    def _learner_registration_data(self, username=None, **kwargs):
        data = {
            'first_name': 'Taylor',
            'last_name': 'Learner',
            'grade_and_class': '8A',
            'gender': 'F',
            'password': 'A-Unique-Strong-Password-289!',
            'confirm_password': 'A-Unique-Strong-Password-289!',
        }
        data.update(kwargs)
        return data

    def test_learner_registration_then_learner_sign_in(self):
        data = self._learner_registration_data()

        response = self.client.post(
            reverse('register_learner'), data, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Registration complete')
        self.assertContains(response, 'Your account ID is learnert')
        learner = CustomUser.objects.get(username='learnert')
        self.assertEqual(learner.user_type, '2')
        self.assertEqual(str(learner.student.school_class), 'Grade 8A')
        self.assertIsNone(learner.student.academic_period)
        self.assertFalse(learner.student.teacher_requests.exists())
        self.assertIsNone(learner.student.created_by)
        self.assertFalse(learner.pin_hash)
        self.assertFalse(learner.profile_pic)
        self.assertTrue(learner.check_password(data['password']))
        self.assertFalse(learner.email)
        self.assertNotIn('_auth_user_id', self.client.session)

        response = self.client.post(reverse('user_login'), {
            'account_id': learner.username,
            'password': data['password'],
            'role': 'learner',
        })
        self.assertRedirects(
            response,
            reverse('student_home'),
            fetch_redirect_response=False,
        )

    def test_account_id_is_generated_and_not_editable_during_registration(self):
        response = self.client.get(reverse('register_learner'))
        self.assertNotContains(response, 'Username')
        self.assertNotContains(response, 'name="username"')

        data = self._learner_registration_data(username='chosen-by-user')
        response = self.client.post(reverse('register_learner'), data)
        self.assertRedirects(
            response, reverse('role_login', args=['learner']))
        learner = CustomUser.objects.get(first_name='Taylor', last_name='Learner')
        self.assertEqual(learner.username, 'learnert')
        self.assertNotEqual(learner.username, 'chosen-by-user')

    def test_password_confirmation_and_minimum_length_validation(self):
        data = self._learner_registration_data(
            confirm_password='A-different-password-289!')
        response = self.client.post(
            reverse('register_learner'), data)
        self.assertContains(response, 'Passwords do not match')

        data = self._learner_registration_data(
            username='weak-password',
            password='short',
            confirm_password='short',
        )
        response = self.client.post(
            reverse('register_learner'), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, 'Password must be at least 8 characters long.')
        self.assertFalse(CustomUser.objects.filter(
            first_name='Taylor', last_name='Learner').exists())

    def test_eight_character_password_accepts_any_character_combination(self):
        data = self._learner_registration_data(
            username='numeric-password',
            password='12345678',
            confirm_password='12345678',
        )
        response = self.client.post(
            reverse('register_learner'), data)

        self.assertRedirects(
            response,
            reverse('role_login', args=['learner']),
        )
        learner = CustomUser.objects.get(username='learnert')
        self.assertTrue(learner.check_password('12345678'))

    def test_registration_preserves_spaces_in_passwords(self):
        password = '  12345 '
        data = self._learner_registration_data(
            username='spaced-password',
            password=password,
            confirm_password=password,
        )
        response = self.client.post(
            reverse('register_learner'), data)

        self.assertRedirects(
            response,
            reverse('role_login', args=['learner']),
        )
        learner = CustomUser.objects.get(username='learnert')
        self.assertTrue(learner.check_password(password))

    def test_registration_page_shows_prominent_crest_and_live_password_feedback(self):
        response = self.client.get(reverse('register_learner'))

        self.assertContains(response, 'C. Heuva school crest')
        self.assertContains(
            response, 'main_app/js/registration-validation.js')
        self.assertContains(response, 'password-length-feedback')
        self.assertContains(response, 'password-match-feedback')
        self.assertContains(
            response, 'At least 8 characters. Any characters are allowed.')

    def test_learner_registration_shows_and_requires_a_class(self):
        response = self.client.get(reverse('register_learner'))
        self.assertContains(response, 'Grade and class')
        self.assertContains(response, 'Examples: 8A, 8 a, 9')

        data = self._learner_registration_data(username='unassigned-learner')
        data['grade_and_class'] = '8 a'
        response = self.client.post(
            reverse('register_learner'), data)
        self.assertRedirects(
            response,
            reverse('role_login', args=['learner']),
        )
        self.assertEqual(
            str(CustomUser.objects.get(
                username='learnert').student.school_class),
            'Grade 8A',
        )

    def test_class_parser_accepts_supported_values_and_rejects_invalid_values(self):
        for value, expected in (
            ('8A', (8, 'a')),
            ('8 a', (8, 'a')),
            ('9', (9, '')),
            ('12G', (12, 'g')),
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_class_label(value), expected)

        for value in ('13A', '8H'):
            with self.subTest(value=value):
                with self.assertRaises(ClassLabelError):
                    parse_class_label(value)

    def test_registration_rejects_invalid_class_values(self):
        for value in ('13A', '8H'):
            with self.subTest(value=value):
                data = self._learner_registration_data(
                    username=f'invalid-class-{value.lower()}',
                    grade_and_class=value,
                )
                response = self.client.post(
                    reverse('register_learner'), data)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(
                    response.context['form'].errors.get('grade_and_class'))
                self.assertFalse(CustomUser.objects.filter(
                    first_name='Taylor', last_name='Learner').exists())

    def test_account_id_generation_uses_last_name_and_first_initial(self):
        data = self._learner_registration_data(
            first_name='Émilie', last_name="O'Connor")
        response = self.client.post(reverse('register_learner'), data)
        self.assertRedirects(
            response, reverse('role_login', args=['learner']))
        learner = CustomUser.objects.get(first_name='Émilie')
        self.assertEqual(learner.username, 'oconnore')

    def test_existing_account_ids_are_rekeyed_without_changing_passwords(self):
        from importlib import import_module
        from types import SimpleNamespace

        from django.apps import apps
        from django.db import connection

        first = CustomUser.objects.create_user(
            username='legacy-first-id',
            password='valid-password',
            user_type='2',
            gender='F',
            first_name='Ada',
            last_name='Lovelace',
        )
        second = CustomUser.objects.create_user(
            username='legacy-second-id',
            password='another-valid-password',
            user_type='2',
            gender='F',
            first_name='Amelia',
            last_name='Lovelace',
        )
        old_first_password = first.password
        old_second_password = second.password
        log_entry = ActivityLog.objects.create(
            actor=first,
            actor_username='legacy-first-id',
            target_user=second,
            target_username='legacy-second-id',
            action='update',
            description='Legacy account ID migration test',
        )

        migration = import_module(
            'main_app.migrations.'
            '0027_alter_customuser_username_reportcardpublication')
        migration.assign_account_ids(
            apps, SimpleNamespace(connection=connection))

        first.refresh_from_db()
        second.refresh_from_db()
        log_entry.refresh_from_db()
        self.assertEqual(first.username, 'lovelacea')
        self.assertEqual(second.username, 'lovelacea2')
        self.assertEqual(first.password, old_first_password)
        self.assertEqual(second.password, old_second_password)
        self.assertEqual(log_entry.actor_username, first.username)
        self.assertEqual(log_entry.target_username, second.username)

    def test_username_command_creates_unique_names_and_csv(self):
        first = CustomUser.objects.create_user(
            email='ab@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            first_name='Ava',
            last_name='Smith',
        )
        second = CustomUser.objects.create_user(
            email='AB@another.example',
            password='valid-password',
            user_type='3',
            gender='M',
            first_name='Amir',
            last_name='Smith',
        )
        CustomUser.objects.filter(pk__in=[first.pk, second.pk]).update(
            username=None)

        with tempfile.TemporaryDirectory() as directory:
            csv_path = f'{directory}\\account-ids.csv'
            call_command('create_usernames', csv_path, stdout=StringIO())

            first.refresh_from_db()
            second.refresh_from_db()
            self.assertEqual(first.username, 'smitha')
            self.assertEqual(second.username, 'smitha2')
            with open(csv_path, newline='', encoding='utf-8') as csv_file:
                rows = csv_file.read().splitlines()
            self.assertEqual(rows[0], 'name,role,account_id')
            self.assertEqual(len(rows), 3)

    def test_create_admin_command_creates_account_and_displays_pin_once(self):
        with patch(
            'builtins.input',
            side_effect=['Alex', 'Administrator', 'F'],
        ):
            output = StringIO()
            call_command('create_admin', stdout=output)

        admin = CustomUser.objects.get(username='administratora')
        self.assertEqual(admin.user_type, '1')
        self.assertEqual(admin.first_name, 'Alex')
        self.assertEqual(admin.last_name, 'Administrator')
        self.assertTrue(Admin.objects.filter(admin=admin).exists())
        self.assertFalse(admin.has_usable_password())
        self.assertTrue(check_password(
            output.getvalue().split('One-time PIN: ', 1)[1].strip(),
            admin.pin_hash,
        ))
        self.assertEqual(output.getvalue().count('One-time PIN:'), 1)

    def test_legacy_email_sign_in_can_be_disabled(self):
        account = CustomUser.objects.create_user(
            email='legacy@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
        )
        self.assertIsNone(account.username)

        with override_settings(SIMS_ALLOW_EMAIL_SIGNIN=True):
            response = self.client.post(reverse('user_login'), {
                'username': 'legacy@example.com',
                'password': 'valid-password',
                'role': 'learner',
            })
        self.assertRedirects(
            response,
            reverse('student_home'),
            fetch_redirect_response=False,
        )
        self.client.logout()

        with override_settings(SIMS_ALLOW_EMAIL_SIGNIN=False):
            response = self.client.post(reverse('user_login'), {
                'username': 'legacy@example.com',
                'password': 'valid-password',
                'role': 'learner',
            })
        self.assertRedirects(
            response,
            reverse('role_login', args=['learner']),
            fetch_redirect_response=False,
        )
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_learner_registration_is_available_without_database_grades(self):
        response = self.client.get(reverse('register_learner'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Registration is not available')
        self.assertNotContains(response, ' disabled')

    def test_registration_rejects_invalid_profile_image(self):
        data = self._learner_registration_data(username='invalid-image')
        data['profile_pic'] = SimpleUploadedFile(
            'photo.jpg', b'this is not an image', content_type='image/jpeg')

        response = self.client.post(
            reverse('register_learner'),
            data,
            format='multipart',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Upload a valid image')
        self.assertFalse(CustomUser.objects.filter(
            first_name='Taylor', last_name='Learner').exists())

    def test_staff_registration_urls_return_404(self):
        for role in ('admin', 'teacher'):
            with self.subTest(role=role):
                response = self.client.get(f'/register/{role}/')
                self.assertEqual(response.status_code, 404)

    def test_staff_sign_in_pages_do_not_link_to_registration(self):
        for role in ('admin', 'teacher'):
            with self.subTest(role=role):
                response = self.client.get(
                    reverse('role_login', args=[role]))
                self.assertEqual(response.status_code, 200)
                info = InterfaceSelectionTests._role_markup(
                    response, 'info', role)
                self.assertNotIn('Register', info)
                self.assertNotIn('/register/', info)

    def test_learner_sign_in_page_links_to_learner_registration(self):
        response = self.client.get(
            reverse('role_login', args=['learner']))
        self.assertContains(response, "Don't have an account? Register")
        self.assertContains(response, reverse('register_learner'))

        response = self.client.get(reverse('register_learner'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse('role_login', args=['learner']),
        )

    def test_signed_in_user_is_redirected_from_registration(self):
        admin = CustomUser.objects.create_user(
            email='already-signed-in@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        self.client.force_login(admin)

        response = self.client.get(reverse('register_learner'))

        self.assertRedirects(
            response,
            reverse('admin_home'),
            fetch_redirect_response=False,
        )

    def test_registered_roles_cannot_sign_in_through_the_other_role(self):
        learner_data = self._learner_registration_data(
            username='role-check-learner')
        learner_registration = self.client.post(
            reverse('register_learner'), learner_data)
        response = self.client.post(reverse('user_login'), {
            'account_id': 'learnert',
            'password': learner_data['password'],
            'role': 'admin',
        })
        self.assertRedirects(
            response,
            reverse('role_login', args=['admin']),
            fetch_redirect_response=False,
        )
        self.assertNotIn('_auth_user_id', self.client.session)

        admin = CustomUser.objects.create_user(
            username='role-check-admin',
            password='A-Unique-Strong-Password-289!',
            user_type='1',
            gender='M',
        )
        admin_pin = issue_pin(admin)
        response = self.client.post(reverse('user_login'), {
            'username': admin.username,
            'password': 'A-Unique-Strong-Password-289!',
            'pin': admin_pin,
            'role': 'learner',
        })
        self.assertRedirects(
            response,
            reverse('role_login', args=['learner']),
            fetch_redirect_response=False,
        )
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_registration_attempts_are_rate_limited_per_client(self):
        from django.test import RequestFactory
        from .views import _registration_rate_limited

        request = RequestFactory().post(
            '/register/learner/', REMOTE_ADDR='192.0.2.55')
        self.assertEqual(
            [_registration_rate_limited(request) for _ in range(6)],
            [False, False, False, False, False, True],
        )

    def test_learner_profile_can_change_class_but_not_email_or_period(self):
        learner = CustomUser.objects.create_user(
            email='restricted-learner@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        own_grade = SchoolClass.objects.create(grade_number=7)
        next_grade = SchoolClass.objects.create(
            grade_number=9, class_letter='b')
        teacher = CustomUser.objects.create_user(
            username='profile-request-teacher',
            password='valid-password',
            user_type='3',
            gender='M',
        )
        TeacherClassSetup.objects.create(
            teacher=teacher.teacher, school_class=own_grade)
        TeacherClassSetup.objects.create(
            teacher=teacher.teacher, school_class=next_grade)
        own_session = AcademicPeriod.objects.create(
            start_year='2026-01-01', end_year='2026-12-31')
        forged_session = AcademicPeriod.objects.create(
            start_year='2027-01-01', end_year='2027-12-31')
        learner.student.school_class = own_grade
        learner.student.academic_period = own_session
        learner.student.save()
        accept_learner_for_teacher(learner.student, teacher.teacher)
        self.client.force_login(learner)

        response = self.client.post(reverse('student_view_profile'), {
            'first_name': 'Changed',
            'last_name': 'Name',
            'email': 'attacker@example.com',
            'grade_and_class': '9B',
            'academic_period': forged_session.pk,
        })

        self.assertRedirects(
            response,
            reverse('student_view_profile'),
            fetch_redirect_response=False,
        )
        learner.refresh_from_db()
        learner.student.refresh_from_db()
        self.assertEqual(learner.email, 'restricted-learner@example.com')
        self.assertEqual(learner.student.school_class.grade_number, 9)
        self.assertEqual(learner.student.school_class.class_letter, 'b')
        self.assertEqual(learner.student.academic_period, own_session)
        self.assertEqual(learner.first_name, 'Changed')
        self.assertTrue(learner.student.teacher_requests.filter(
            school_class=own_grade,
            status=LearnerTeacherRequest.CANCELLED,
        ).exists())
        self.assertTrue(learner.student.teacher_requests.filter(
            school_class=next_grade,
            status=LearnerTeacherRequest.PENDING,
        ).exists())

    def test_admin_profile_ignores_submitted_email(self):
        admin = CustomUser.objects.create_user(
            email='restricted-admin@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
            first_name='Restricted',
            last_name='Administrator',
        )
        self.client.force_login(admin)

        response = self.client.post(reverse('admin_view_profile'), {
            'first_name': 'Changed',
            'last_name': 'Admin',
            'email': 'attacker@example.com',
        })

        self.assertRedirects(
            response,
            reverse('admin_view_profile'),
            fetch_redirect_response=False,
        )
        admin.refresh_from_db()
        self.assertEqual(admin.email, 'restricted-admin@example.com')

    def test_profile_pages_are_limited_to_the_signed_in_users_role(self):
        learner = CustomUser.objects.create_user(
            email='profile-owner-learner@example.com',
            username='profile-owner-learner',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        admin = CustomUser.objects.create_user(
            email='profile-owner-admin@example.com',
            username='profile-owner-admin',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        self.client.force_login(learner)
        response = self.client.get(reverse('admin_view_profile'))
        self.assertRedirects(
            response,
            reverse('student_home'),
            fetch_redirect_response=False,
        )
        response = self.client.get(reverse('student_view_profile'))
        self.assertEqual(response.status_code, 200)

        self.client.force_login(admin)
        response = self.client.get(reverse('student_view_profile'))
        self.assertRedirects(
            response,
            reverse('admin_home'),
            fetch_redirect_response=False,
        )
        response = self.client.get(reverse('admin_view_profile'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'My Profile')
        self.assertContains(response, admin.username)
        self.assertNotContains(response, 'name="email"')

    def test_profile_password_change_requires_current_password_and_keeps_session(self):
        learner = CustomUser.objects.create_user(
            email='password-profile@example.com',
            password='Original-Strong-Password-789!',
            user_type='2',
            gender='F',
            address='',
            first_name='Password',
            last_name='Learner',
        )
        self.client.force_login(learner)
        profile_url = reverse('student_view_profile')
        response = self.client.post(profile_url, {
            'first_name': learner.first_name,
            'last_name': learner.last_name,
            'current_password': 'wrong-current-password',
            'new_password': 'A-Different-Strong-Password-789!',
            'confirm_password': 'A-Different-Strong-Password-789!',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Current password is incorrect')
        learner.refresh_from_db()
        self.assertTrue(learner.check_password('Original-Strong-Password-789!'))

        response = self.client.post(profile_url, {
            'first_name': learner.first_name,
            'last_name': learner.last_name,
            'current_password': 'Original-Strong-Password-789!',
            'new_password': 'A-Different-Strong-Password-789!',
            'confirm_password': 'A-Different-Strong-Password-789!',
        })
        self.assertRedirects(
            response, profile_url, fetch_redirect_response=False)
        learner.refresh_from_db()
        self.assertTrue(learner.check_password('A-Different-Strong-Password-789!'))
        self.assertEqual(self.client.get(profile_url).status_code, 200)

    def test_profile_picture_can_be_updated_for_both_roles(self):
        learner = CustomUser.objects.create_user(
            email='picture-learner@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        admin = CustomUser.objects.create_user(
            email='picture-admin@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                for user, profile_url in (
                        (learner, reverse('student_view_profile')),
                        (admin, reverse('admin_view_profile'))):
                    with self.subTest(role=user.user_type):
                        self.client.force_login(user)
                        response = self.client.post(profile_url, {
                            'first_name': 'Updated',
                            'last_name': 'Picture',
                            'profile_pic': self._profile_image_upload(),
                        })
                        self.assertRedirects(
                            response, profile_url, fetch_redirect_response=False)
                        user.refresh_from_db()
                        self.assertTrue(user.profile_pic.name)
                        self.assertTrue(user.profile_pic.name.endswith('.png'))

    @staticmethod
    def _profile_image_upload():
        image_data = BytesIO()
        Image.new('RGB', (2, 2), color='white').save(image_data, format='PNG')
        return SimpleUploadedFile(
            'profile.png', image_data.getvalue(), content_type='image/png')


@override_settings(PASSWORD_HASHERS=[
    'django.contrib.auth.hashers.MD5PasswordHasher',
])
class PINAndLockoutTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = CustomUser.objects.create_user(
            username='pin-teacher',
            password='A-Strong-Password-283!',
            user_type='3',
            gender='F',
            first_name='PIN',
            last_name='Teacher',
        )
        self.legacy_learner = CustomUser.objects.create_user(
            username='legacy-learner-pin',
            password='A-Strong-Password-283!',
            user_type='2',
            gender='F',
        )
        issue_pin(self.legacy_learner)
        self.pin = issue_pin(self.user)

    def _sign_in(self, **overrides):
        data = {
            'username': self.user.username,
            'pin': self.pin,
            'role': 'teacher',
        }
        data.update(overrides)
        return self.client.post(reverse('user_login'), data)

    def test_staff_sign_in_uses_a_hashed_pin_without_a_password(self):
        self.user.refresh_from_db()
        self.assertNotEqual(self.user.pin_hash, self.pin)
        self.assertTrue(check_password(self.pin, self.user.pin_hash))
        self.assertFalse(self.user.has_usable_password())
        response = self._sign_in(pin='')
        self.assertRedirects(
            response,
            reverse('role_login', args=['teacher']),
            fetch_redirect_response=False,
        )
        self.assertNotIn('_auth_user_id', self.client.session)

        response = self._sign_in()
        self.assertRedirects(
            response,
            reverse('teacher_home'),
            fetch_redirect_response=False,
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.failed_login_attempts, 0)
        self.assertIsNone(self.user.locked_until)

    def test_pin_generator_rejects_weak_values(self):
        with patch(
            'main_app.credentials.secrets.randbelow',
            side_effect=[0, 11111, 12345, 54321, 22222, 14256],
        ):
            self.assertEqual(generate_pin(), '14256')

    def test_validate_pin_rejects_wrong_length_and_sequences(self):
        with self.assertRaises(ValueError):
            validate_pin('12345')
        with self.assertRaises(ValueError):
            validate_pin('1234')
        self.assertEqual(validate_pin('90817'), '90817')

    def test_all_sign_in_failures_use_the_same_generic_error(self):
        attempts = (
            ('missing-user', self.pin, 'teacher'),
            (self.user.username, '24680', 'teacher'),
            (self.user.username, self.pin, 'admin'),
        )
        for username, pin, role in attempts:
            with self.subTest(username=username, role=role, pin=pin):
                response = self.client.post(reverse('user_login'), {
                    'username': username,
                    'pin': pin,
                    'role': role,
                })
                self.assertRedirects(
                    response,
                    reverse('role_login', args=[role]),
                    fetch_redirect_response=False,
                )
                error_page = self.client.get(
                    reverse('role_login', args=[role]))
                self.assertContains(error_page, 'Invalid sign-in details')
                self.assertNotIn('_auth_user_id', self.client.session)

    @override_settings(
        SIMS_LOGIN_MAX_FAILED_ATTEMPTS=5,
        SIMS_LOGIN_LOCKOUT_MINUTES=15,
    )
    def test_five_failed_attempts_lock_the_account_and_success_unlocks_it(self):
        for _ in range(5):
            self._sign_in(pin='24680')

        self.user.refresh_from_db()
        self.assertEqual(self.user.failed_login_attempts, 5)
        self.assertGreater(self.user.locked_until, timezone.now())

        response = self._sign_in()
        self.assertRedirects(
            response,
            reverse('role_login', args=['teacher']),
            fetch_redirect_response=False,
        )
        self.assertNotIn('_auth_user_id', self.client.session)

        self.user.locked_until = timezone.now() - timedelta(seconds=1)
        self.user.save(update_fields=['locked_until'])
        response = self._sign_in()
        self.assertRedirects(
            response,
            reverse('teacher_home'),
            fetch_redirect_response=False,
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.failed_login_attempts, 0)
        self.assertIsNone(self.user.locked_until)

    @override_settings(SIMS_LOGIN_IP_MAX_FAILURES=2)
    def test_ip_failure_limit_blocks_a_later_correct_sign_in(self):
        self._sign_in(pin='24680')
        self._sign_in(pin='24680')
        response = self._sign_in()

        self.assertRedirects(
            response,
            reverse('role_login', args=['teacher']),
            fetch_redirect_response=False,
        )
        error_page = self.client.get(reverse('role_login', args=['teacher']))
        self.assertContains(error_page, 'Invalid sign-in details')
        self.assertNotIn('_auth_user_id', self.client.session)
        self.user.refresh_from_db()
        self.assertEqual(self.user.failed_login_attempts, 2)

    def test_staff_without_a_pin_cannot_sign_in(self):
        account = CustomUser.objects.create_user(
            username='unissued-pin',
            password=None,
            user_type='3',
            gender='F',
        )
        response = self.client.post(reverse('user_login'), {
            'username': account.username,
            'pin': '',
            'role': 'teacher',
        })
        self.assertRedirects(
            response,
            reverse('role_login', args=['teacher']),
            fetch_redirect_response=False,
        )
        error_page = self.client.get(reverse('role_login', args=['teacher']))
        self.assertContains(error_page, 'Invalid sign-in details')

    def test_issue_pins_command_creates_hashes_and_one_time_csv(self):
        second = CustomUser.objects.create_user(
            username='another-user',
            password='A-Strong-Password-283!',
            user_type='3',
            gender='M',
            first_name='Another',
            last_name='Teacher',
        )

        with tempfile.TemporaryDirectory() as directory:
            csv_path = f'{directory}\\pins.csv'
            output = StringIO()
            call_command('issue_pins', csv_path, stdout=output)

            with open(csv_path, newline='', encoding='utf-8') as csv_file:
                rows = list(csv.reader(csv_file))
            self.assertEqual(rows[0], ['name', 'role', 'account_id', 'PIN'])
            self.assertEqual(len(rows), 3)
            self.assertIn('delete the CSV', output.getvalue())
            self.assertNotIn(self.pin, output.getvalue())

        second.refresh_from_db()
        self.assertTrue(second.pin_hash)
        self.assertFalse(second.has_usable_password())
        self.assertTrue(check_password(rows[2][3], second.pin_hash))
        self.legacy_learner.refresh_from_db()
        self.assertFalse(self.legacy_learner.pin_hash)


@override_settings(PASSWORD_HASHERS=[
    'django.contrib.auth.hashers.MD5PasswordHasher',
])
class CredentialResetTests(TestCase):
    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='reset-admin',
            password='Original-Admin-Password-381!',
            user_type='1',
            gender='M',
        )
        self.teacher = CustomUser.objects.create_user(
            username='reset-teacher',
            password='Original-Teacher-Password-381!',
            user_type='3',
            gender='F',
            first_name='Taylor',
            last_name='Teacher',
        )
        self.learner = CustomUser.objects.create_user(
            username='reset-learner',
            password='Original-Learner-Password-381!',
            user_type='2',
            gender='F',
            first_name='Casey',
            last_name='Learner',
        )
        accept_learner_for_teacher(
            self.learner.student, self.teacher.teacher)
        self.admin_pin = issue_pin(self.admin)
        self.teacher_pin = issue_pin(self.teacher)

    def _reset_url(self, user):
        return reverse('reset_user_credentials', args=[user.pk])

    def test_administrator_can_reset_learner_password(self):
        self.client.force_login(self.admin)
        new_password = 'Different-Strong-Password-932!'
        self.learner.failed_login_attempts = 4
        self.learner.locked_until = timezone.now() + timedelta(minutes=15)
        response = self.client.post(self._reset_url(self.learner), {
            'action': 'password',
            'new_password': new_password,
            'confirm_password': new_password,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, new_password)
        self.learner.refresh_from_db()
        self.assertTrue(self.learner.check_password(new_password))
        self.assertEqual(self.learner.failed_login_attempts, 0)
        self.assertIsNone(self.learner.locked_until)
        self.assertFalse(self.teacher.has_usable_password())

    def test_administrator_and_teacher_can_reset_allowed_pins(self):
        self.client.force_login(self.admin)
        response = self.client.post(self._reset_url(self.teacher), {
            'action': 'pin',
        })
        self.assertEqual(response.status_code, 200)
        new_teacher_pin = response.context['secret']
        self.teacher.refresh_from_db()
        self.assertNotEqual(new_teacher_pin, self.teacher_pin)
        self.assertTrue(check_password(new_teacher_pin, self.teacher.pin_hash))
        self.assertFalse(self.teacher.has_usable_password())

        self.client.force_login(self.teacher)
        response = self.client.post(self._reset_url(self.learner), {
            'action': 'password',
            'new_password': 'Different-Learner-Password-932!',
            'confirm_password': 'Different-Learner-Password-932!',
        })
        self.assertEqual(response.status_code, 200)
        self.learner.refresh_from_db()
        self.assertTrue(self.learner.check_password(
            'Different-Learner-Password-932!'))
        self.assertFalse(self.learner.pin_hash)

    def test_teacher_cannot_reset_teacher_or_administrator(self):
        self.client.force_login(self.teacher)
        response = self.client.get(self._reset_url(self.teacher))
        self.assertEqual(response.status_code, 403)
        response = self.client.get(self._reset_url(self.admin))
        self.assertEqual(response.status_code, 404)
        unassigned_learner = CustomUser.objects.create_user(
            username='unassigned-reset-target',
            password='valid-password',
            user_type='2',
            gender='F',
        )
        response = self.client.get(self._reset_url(unassigned_learner))
        self.assertEqual(response.status_code, 404)

    def test_learner_can_change_password_without_a_pin(self):
        self.client.force_login(self.learner)
        response = self.client.post(reverse('student_view_profile'), {
            'first_name': self.learner.first_name,
            'last_name': self.learner.last_name,
            'current_password': 'Original-Learner-Password-381!',
            'new_password': 'Changed-Learner-Password-853!',
            'confirm_password': 'Changed-Learner-Password-853!',
        })
        self.assertRedirects(
            response, reverse('student_view_profile'),
            fetch_redirect_response=False)
        self.learner.refresh_from_db()
        self.assertTrue(
            self.learner.check_password('Changed-Learner-Password-853!'))
        self.assertEqual(self.client.get(
            reverse('student_view_profile')).status_code, 200)

    def test_teacher_can_reset_own_pin_with_the_current_pin(self):
        self.client.force_login(self.teacher)
        profile_response = self.client.get(reverse('teacher_profile'))
        self.assertContains(profile_response, 'Reset my PIN')
        self.assertContains(
            profile_response,
            'Enter your current PIN to verify your identity')
        response = self.client.post(reverse('teacher_profile'), {
            'first_name': self.teacher.first_name,
            'last_name': self.teacher.last_name,
            'current_pin': self.teacher_pin,
            'reset_pin': 'on',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('issued_pin', response.context)
        self.teacher.refresh_from_db()
        self.assertFalse(self.teacher.has_usable_password())

    def test_self_service_pin_reset_requires_current_pin(self):
        self.client.force_login(self.teacher)

        response = self.client.post(reverse('teacher_profile'), {
            'first_name': self.teacher.first_name,
            'last_name': self.teacher.last_name,
            'reset_pin': 'on',
        })

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('issued_pin', response.context)
        self.assertIn('current_pin', response.context['form'].errors)
        self.teacher.refresh_from_db()
        self.assertTrue(check_password(self.teacher_pin, self.teacher.pin_hash))

    def test_administrator_can_change_own_pin_from_profile(self):
        self.client.force_login(self.admin)
        profile_response = self.client.get(reverse('admin_view_profile'))
        self.assertContains(profile_response, 'Reset my PIN')
        response = self.client.post(reverse('admin_view_profile'), {
            'first_name': self.admin.first_name or 'Administrator',
            'last_name': self.admin.last_name or 'Account',
            'current_pin': self.admin_pin,
            'reset_pin': 'on',
        })

        self.assertEqual(response.status_code, 200)
        new_pin = response.context['issued_pin']
        self.assertContains(response, new_pin)
        self.admin.refresh_from_db()
        self.assertNotEqual(new_pin, self.admin_pin)
        self.assertTrue(check_password(new_pin, self.admin.pin_hash))
        self.assertFalse(self.admin.has_usable_password())

    def test_administrator_can_reset_another_administrators_pin(self):
        other_admin = CustomUser.objects.create_user(
            username='reset-admin-target',
            password=None,
            user_type='1',
            gender='F',
            first_name='Morgan',
            last_name='Administrator',
        )
        old_pin = issue_pin(other_admin)
        self.client.force_login(self.admin)

        response = self.client.get(reverse('manage_admins'))
        self.assertContains(
            response,
            reverse('reset_user_credentials', args=[other_admin.pk]))
        response = self.client.post(self._reset_url(other_admin), {
            'action': 'pin',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['return_url'], reverse('manage_admins'))
        new_pin = response.context['secret']
        self.assertNotEqual(new_pin, old_pin)
        other_admin.refresh_from_db()
        self.assertTrue(check_password(new_pin, other_admin.pin_hash))
        self.assertFalse(other_admin.has_usable_password())

    def test_administrator_cannot_use_account_reset_page_for_own_pin(self):
        self.client.force_login(self.admin)
        response = self.client.get(self._reset_url(self.admin))
        self.assertEqual(response.status_code, 404)
        self.client.logout()
        self.admin.is_staff = True
        self.admin.is_superuser = True
        self.admin.save(update_fields=['is_staff', 'is_superuser'])
        self.client.force_login(self.admin)
        for path in (
            '/admin/password_change/',
            '/admin/password_change/done/',
            f'/admin/main_app/customuser/{self.admin.pk}/password/',
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_email_password_reset_and_email_availability_urls_are_removed(self):
        for path in (
            '/accounts/password_reset/',
            '/accounts/password_reset/done/',
            '/accounts/reset/uid/token/',
            '/accounts/reset/done/',
            '/check_email_availability',
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        response = self.client.get(reverse('role_login', args=['learner']))
        self.assertNotContains(response, 'password_reset')
        self.assertContains(
            response,
            'Forgot your password? Ask your teacher or administrator to reset it.')

    def test_operator_commands_reset_admin_credentials_without_crossing_them(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command(
                'set_admin_password',
                self.admin.username,
                stdout=StringIO(),
            )

        django_superuser = CustomUser.objects.create_superuser(
            username='django-superuser',
            password='Original-Superuser-Password-923!',
            user_type='1',
            gender='M',
        )
        with patch(
            'getpass.getpass',
            side_effect=[
                'New-Operator-Password-625!',
                'New-Operator-Password-625!',
            ],
        ):
            output = StringIO()
            call_command(
                'set_admin_password',
                django_superuser.username,
                stdout=output,
            )
        django_superuser.refresh_from_db()
        self.assertTrue(django_superuser.check_password(
            'New-Operator-Password-625!'))
        self.assertNotIn('New-Operator-Password-625!', output.getvalue())

        output = StringIO()
        call_command(
            'reset_admin_pin',
            self.admin.username,
            stdout=output,
        )
        admin_pin = output.getvalue().strip().split(': ')[-1]
        self.admin.refresh_from_db()
        self.assertTrue(check_password(admin_pin, self.admin.pin_hash))
        self.assertFalse(self.admin.has_usable_password())


class AdministratorStageSixTests(TestCase):
    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='stage-six-admin',
            password='Valid-Admin-Password-399!',
            user_type='1',
            gender='F',
        )
        self.client.force_login(self.admin)

    def test_school_profile_is_admin_editable(self):
        response = self.client.get(reverse('school_profile'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'School Profile')
        self.assertContains(response, 'postal_address')

        response = self.client.post(reverse('school_profile'), {
            'school_name': 'C. Heuva Junior Secondary School',
            'postal_address': 'Private Bag 12',
            'phone_numbers': '555-0100',
            'motto': '',
            'principal_name': 'A. Principal',
        })
        self.assertRedirects(
            response, reverse('school_profile'),
            fetch_redirect_response=False)
        profile = SchoolProfile.objects.get(pk=1)
        self.assertEqual(profile.postal_address, 'Private Bag 12')
        self.assertEqual(profile.phone_numbers, '555-0100')
        self.assertEqual(profile.motto, '')

    def test_notify_all_saves_in_app_notifications_for_every_learner(self):
        learner_a = CustomUser.objects.create_user(
            username='broadcast-learner-a',
            password='Valid-Learner-Password-399!',
            user_type='2',
            gender='F',
        )
        learner_b = CustomUser.objects.create_user(
            username='broadcast-learner-b',
            password='Valid-Learner-Password-400!',
            user_type='2',
            gender='M',
        )
        self.assertContains(
            self.client.get(reverse('admin_notify_student')),
            'Notify all learners (2)',
        )

        response = self.client.post(reverse('send_student_notification'), {
            'id': 'all',
            'message': 'School notice',
        })

        self.assertEqual(response.content, b'True')
        self.assertEqual(
            NotificationStudent.objects.filter(
                student__admin__in=[learner_a, learner_b],
                message='School notice',
            ).count(),
            2,
        )

    def test_notify_all_endpoint_is_administrator_only(self):
        teacher = CustomUser.objects.create_user(
            username='stage-six-teacher',
            password='Valid-Teacher-Password-399!',
            user_type='3',
            gender='M',
        )
        self.client.force_login(teacher)

        response = self.client.post(reverse('send_student_notification'), {
            'id': 'all',
            'message': 'School notice',
        })

        self.assertEqual(response.status_code, 302)
        self.assertFalse(NotificationStudent.objects.exists())

    def test_activity_sidebar_item_is_marked_current(self):
        response = self.client.get(reverse('activity_log'))
        self.assertContains(
            response,
            'class="nav-link active" aria-current="page"',
        )

    def test_sidebar_highlights_teacher_and_learner_sections(self):
        teacher = CustomUser.objects.create_user(
            username='stage-six-teacher-nav',
            password='Valid-Teacher-Password-400!',
            user_type='3',
            gender='M',
        )
        learner = CustomUser.objects.create_user(
            username='stage-six-learner-nav',
            password='Valid-Learner-Password-401!',
            user_type='2',
            gender='F',
        )
        for user, route, active_href in (
            (teacher, 'teacher_classes_subjects', '/teacher/classes-subjects/'),
            (learner, 'student_view_result', '/student/view/result/'),
        ):
            with self.subTest(role=user.user_type):
                self.client.force_login(user)
                response = self.client.get(reverse(route))
                self.assertContains(response, 'aria-current="page"')
                self.assertContains(response, f'href="{active_href}" class="nav-link active"')


class ActivityLogTests(TestCase):
    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='activity-admin',
            password='Valid-Admin-Password-389!',
            user_type='1',
            gender='F',
        )

    def test_model_changes_include_actor_and_assignment_values_without_secrets(self):
        request = RequestFactory().post('/admin/assignments/')
        request.user = self.admin
        token = set_current_request(request)
        try:
            teacher_user = CustomUser.objects.create_user(
                username='activity-teacher',
                password='Never-Log-This-Password-391!',
                user_type='3',
                gender='M',
                first_name='Casey',
                last_name='Teacher',
            )
            teacher_pin = issue_pin(teacher_user)
            learner_user = CustomUser.objects.create_user(
                username='activity-learner',
                password='Never-Log-Learner-Password-392!',
                user_type='2',
                gender='F',
            )
            learner = learner_user.student
            accept_learner_for_teacher(learner, teacher_user.teacher)
        finally:
            reset_current_request(token)

        assignment = ActivityLog.objects.filter(
            object_type='main_app.learnerteacherrequest',
            target_user=learner_user,
            action='create',
        ).latest('created_at')
        self.assertEqual(assignment.actor_id, self.admin.pk)
        self.assertEqual(assignment.target_user_id, learner_user.pk)
        self.assertEqual(
            assignment.changes['status']['new'],
            LearnerTeacherRequest.ACCEPTED,
        )
        self.assertIn(
            'Casey Teacher', assignment.changes['teacher']['new'])

        user_events = ActivityLog.objects.filter(
            object_type='main_app.customuser',
            object_id=str(teacher_user.pk),
        )
        serialized = ' '.join(str(event.changes) for event in user_events)
        self.assertNotIn('Never-Log-This-Password-391!', serialized)
        self.assertNotIn(teacher_pin, serialized)
        self.assertIn('[redacted]', serialized)

    def test_admin_can_filter_export_and_view_a_user_timeline(self):
        learner = CustomUser.objects.create_user(
            username='activity-target',
            password='Valid-Learner-Password-391!',
            user_type='2',
            gender='F',
        )
        event = ActivityLog.objects.create(
            actor=self.admin,
            actor_username=self.admin.username,
            target_user=learner,
            action='update',
            object_type='main_app.student',
            object_id=str(learner.student.pk),
            description='Updated activity target learner',
            changes={'teacher': {'old': None, 'new': 'Assigned Teacher'}},
        )
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse('activity_log'),
            {'search': 'activity target', 'action': 'update'},
        )
        self.assertContains(response, 'teacher from blank to Assigned Teacher')
        self.assertNotContains(response, 'Changes')

        role_filtered = self.client.get(
            reverse('activity_log'),
            {'role': '2', 'action': 'update'},
        )
        self.assertContains(role_filtered, 'activity-target')
        self.assertContains(role_filtered, 'Learner')

        response = self.client.get(
            reverse('activity_log'), {'action': 'update', 'export': 'csv'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/csv', response['Content-Type'])
        self.assertContains(response, 'teacher from blank to Assigned Teacher')

        response = self.client.get(
            reverse('activity_user_timeline', args=[learner.pk]))
        self.assertContains(response, 'teacher from blank to Assigned Teacher')

        for export_format in ('csv', 'pdf'):
            with self.subTest(timeline_export=export_format):
                timeline_export = self.client.get(
                    reverse('activity_user_timeline', args=[learner.pk]),
                    {'action': 'update', 'export': export_format},
                )
                self.assertEqual(timeline_export.status_code, 200)
                if export_format == 'csv':
                    self.assertIn(b'Assigned Teacher', timeline_export.content)
                else:
                    self.assertTrue(timeline_export.content.startswith(b'%PDF'))

        teacher = CustomUser.objects.create_user(
            username='activity-nonadmin',
            password='Valid-Teacher-Password-391!',
            user_type='3',
            gender='M',
        )
        self.client.force_login(teacher)
        response = self.client.get(reverse('activity_log'))
        self.assertRedirects(
            response, reverse('teacher_home'),
            fetch_redirect_response=False)

    def test_activity_page_includes_legacy_learner_signins(self):
        learner = CustomUser.objects.create_user(
            username='legacy-login-learner',
            password='Valid-Learner-Password-399!',
            user_type='2',
            gender='F',
        )
        LoginActivity.objects.create(student=learner.student)
        self.client.force_login(self.admin)

        response = self.client.get(reverse('activity_log'), {
            'role': '2',
            'action': 'sign_in',
        })

        self.assertContains(response, 'legacy-login-learner signed in')
        self.assertContains(response, 'Learner')
        self.assertNotContains(response, 'Changes')
        self.assertRedirects(
            self.client.get(reverse('learner_login_activity')),
            reverse('activity_log'),
            fetch_redirect_response=False,
        )

    def test_activity_sentence_describes_assessment_mark_changes(self):
        teacher_user = CustomUser.objects.create_user(
            username='activity-mark-teacher',
            password='Valid-Teacher-Password-399!',
            user_type='3',
            gender='M',
            first_name='Maria',
            last_name='K.',
        )
        learner_user = CustomUser.objects.create_user(
            username='activity-mark-learner',
            password='Valid-Learner-Password-399!',
            user_type='2',
            gender='F',
            first_name='Selma',
            last_name='I.',
        )
        school_class = SchoolClass.objects.create(
            grade_number=8, class_letter='C')
        learner = learner_user.student
        learner.school_class = school_class
        learner.save(update_fields=['school_class'])
        subject = Subject.objects.create(
            name='Mathematics',
            teacher=teacher_user.teacher,
            school_class=school_class,
        )
        period = AcademicPeriod.objects.create(
            year=2026, term=1, is_current=True)
        plan = AssessmentPlan.objects.create(
            school_class=school_class,
            subject=subject,
            academic_period=period,
        )
        assessment = Assessment.objects.create(
            plan=plan, name='Test 1', weight=20, maximum_mark=100)
        result = StudentResult.objects.create(
            student=learner,
            subject=subject,
            academic_period=period,
            assessment_scores={str(assessment.pk): '52'},
        )
        created_event = ActivityLog.objects.get(
            action='create',
            object_type='main_app.studentresult',
            object_id=str(result.pk),
        )
        self.assertIsInstance(
            created_event.changes['assessment_scores']['new'], dict)
        event = ActivityLog.objects.create(
            actor=teacher_user,
            actor_username='Teacher Maria K.',
            action='update',
            object_type='main_app.studentresult',
            object_id=str(result.pk),
            changes={
                'assessment_scores': {
                    'old': {str(assessment.pk): '45'},
                    'new': {str(assessment.pk): '52'},
                },
            },
        )

        from .hod_views import _activity_sentence

        sentence = _activity_sentence(event)
        self.assertIn(
            'Teacher Maria K. changed Test 1 from 45 to 52', sentence)
        self.assertIn('Selma I. in Grade 8C Mathematics', sentence)

        legacy_event = ActivityLog.objects.create(
            actor_username='Legacy uploader',
            action='create',
            object_type='main_app.studentresult',
            object_id=str(result.pk),
            changes={
                'assessment_scores': {
                    'old': None,
                    'new': f"{{'{assessment.pk}': '52'}}",
                },
            },
        )
        legacy_sentence = _activity_sentence(legacy_event)
        self.assertIn(
            'Legacy uploader entered Test 1 from blank to 52',
            legacy_sentence,
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse('activity_log'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'Legacy uploader entered Test 1 from blank to 52',
        )

    def test_deleting_target_account_keeps_a_valid_user_snapshot(self):
        learner = CustomUser.objects.create_user(
            username='activity-deleted-target',
            password='Valid-Learner-Password-392!',
            user_type='2',
            gender='F',
        )
        learner_id = str(learner.pk)
        learner.delete()

        deletion_events = ActivityLog.objects.filter(
            target_user_id_snapshot=learner_id,
            action='delete',
        )
        self.assertGreaterEqual(deletion_events.count(), 2)
        self.assertTrue(all(
            event.target_user_id is None
            for event in deletion_events
        ))
        self.assertTrue(all(
            event.target_username == 'activity-deleted-target'
            for event in deletion_events
        ))

    @override_settings(SIMS_ACTIVITY_LOG_RETENTION_DAYS=30)
    def test_retention_command_removes_only_entries_past_the_cutoff(self):
        old_entry = ActivityLog.objects.create(
            actor_username='old-user',
            action='delete',
            object_type='main_app.student',
            description='Old entry',
        )
        recent_entry = ActivityLog.objects.create(
            actor_username='recent-user',
            action='create',
            object_type='main_app.student',
            description='Recent entry',
        )
        ActivityLog.objects.filter(pk=old_entry.pk).update(
            created_at=timezone.now() - timedelta(days=31))

        call_command('prune_activity_log', stdout=StringIO())

        self.assertFalse(ActivityLog.objects.filter(pk=old_entry.pk).exists())
        self.assertTrue(ActivityLog.objects.filter(pk=recent_entry.pk).exists())


@override_settings(PASSWORD_HASHERS=[
    'django.contrib.auth.hashers.MD5PasswordHasher',
])
class DefaultAccountCommandTests(TestCase):
    @staticmethod
    def _create_accounts(**options):
        output = StringIO()
        call_command('create_default_accounts', stdout=output, **options)
        return output.getvalue()

    def test_command_creates_distinct_role_accounts_with_generated_secrets(self):
        output = self._create_accounts()
        accounts = {
            account.username: account
            for account in CustomUser.objects.filter(
                username__in=(
                    'administrators',
                    'teachers',
                    'learners',
                )
            )
        }
        self.assertEqual(len(accounts), 3)
        admin = accounts['administrators']
        teacher = accounts['teachers']
        learner = accounts['learners']
        self.assertEqual(admin.user_type, '1')
        self.assertEqual(teacher.user_type, '3')
        self.assertEqual(learner.user_type, '2')
        self.assertFalse(admin.has_usable_password())
        self.assertFalse(teacher.has_usable_password())
        self.assertTrue(learner.has_usable_password())
        self.assertTrue(learner.check_password(
            output.split('Learner: account_id=learners, password=', 1)[1]
            .splitlines()[0]))
        self.assertIn('Administrator: account_id=administrators, PIN=', output)
        self.assertIn('Teacher: account_id=teachers, PIN=', output)
        admin_pin = output.split(
            'Administrator: account_id=administrators, PIN=', 1)[1].splitlines()[0]
        teacher_pin = output.split(
            'Teacher: account_id=teachers, PIN=', 1)[1].splitlines()[0]
        self.assertNotEqual(admin_pin, teacher_pin)
        self.assertEqual(
            [admin.is_superuser, teacher.is_superuser, learner.is_superuser],
            [False, False, False],
        )
    def test_account_forms_do_not_expose_an_editable_account_id(self):
        from .forms import (
            LearnerRegistrationForm,
            StudentForm,
            TeacherAccountForm,
        )

        for form in (
            LearnerRegistrationForm(),
            StudentForm(),
            TeacherAccountForm(),
        ):
            with self.subTest(form=type(form).__name__):
                self.assertNotIn('username', form.fields)

    def test_new_accounts_with_matching_names_receive_numeric_suffixes(self):
        first = CustomUser.objects.create_user(
            password='valid-password',
            user_type='2',
            gender='F',
            first_name='Ada',
            last_name='Lovelace',
        )
        second = CustomUser.objects.create_user(
            password='another-valid-password',
            user_type='2',
            gender='F',
            first_name='Amelia',
            last_name='Lovelace',
        )

        self.assertEqual(first.username, 'lovelacea')
        self.assertEqual(second.username, 'lovelacea2')

    def test_command_refuses_to_overwrite_existing_account(self):
        CustomUser.objects.create_user(
            password='A-Valid-Learner-Password-927!',
            user_type='2',
            gender='F',
            first_name='SIMS',
            last_name='Learner',
        )
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command('create_default_accounts', stdout=StringIO())
        self.assertFalse(
            CustomUser.objects.filter(username='administrators').exists())

    def test_command_can_reuse_hidden_credentials_across_environments(self):
        with patch(
            'main_app.management.commands.create_default_accounts.getpass.getpass',
            side_effect=[
                'Strong-Shared-Learner-Pass-782!',
                '48291',
                '73914',
            ],
        ):
            output = self._create_accounts(reuse_credentials=True)

        self.assertIn(
            'Administrator: account_id=administrators, PIN=48291', output)
        self.assertIn(
            'Teacher: account_id=teachers, PIN=73914', output)
        learner = CustomUser.objects.get(username='learners')
        self.assertTrue(learner.check_password(
            'Strong-Shared-Learner-Pass-782!'))


@override_settings(PASSWORD_HASHERS=[
    'django.contrib.auth.hashers.MD5PasswordHasher',
])
class LearnerImportAndExportTests(TestCase):
    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='export-admin',
            password='valid-password',
            user_type='1',
            gender='F',
        )
        self.teacher_user = CustomUser.objects.create_user(
            username='export-teacher',
            password='valid-password',
            user_type='3',
            gender='M',
        )
        self.period = AcademicPeriod.objects.create(
            year=2026, term=1, is_current=True)
        self.school_class = SchoolClass.objects.create(grade_number=7)
        TeacherClassSetup.objects.create(
            teacher=self.teacher_user.teacher,
            school_class=self.school_class,
        )
        self.client.force_login(self.admin)

    @staticmethod
    def _csv_file(rows, name='learners.csv'):
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            'first_name', 'last_name', 'gender', 'class',
        ])
        writer.writerows(rows)
        return SimpleUploadedFile(
            name, buffer.getvalue().encode('utf-8'), content_type='text/csv')

    def _preview_csv(self, rows, name='learners.csv'):
        return self.client.post(
            reverse('import_learners'),
            {
                'action': 'upload',
                'file': self._csv_file(rows, name=name),
            },
        )

    def _confirm_import(self, batch):
        return self.client.post(reverse('import_learners'), {
            'action': 'confirm',
            'import_token': str(batch.token),
            'academic_period': self.period.pk,
        })

    def _finish_import(self, batch):
        response = None
        while batch.status == LearnerImportBatch.PROCESSING:
            response = self.client.post(reverse(
                'process_learner_import',
                kwargs={'token': batch.token},
            ))
            self.assertEqual(response.status_code, 200)
            batch.refresh_from_db()
        return response

    def test_csv_import_previews_then_creates_learners_with_one_time_passwords(self):
        response = self._preview_csv([[
            'Ada', 'Okafor', 'F', 'Grade 7',
        ]])

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Preview (1 learner rows)')
        self.assertContains(response, 'Ready')
        batch = response.context['preview_batch']
        self.assertNotIn('learner_import_preview', self.client.session)

        response = self._confirm_import(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.status, LearnerImportBatch.PROCESSING)
        self.assertContains(response, '0/1 learners recorded')
        self._finish_import(batch)

        response = self.client.get(reverse(
            'complete_learner_import',
            kwargs={'token': batch.token},
        ))
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertContains(response, 'until you confirm that you have saved')
        self.assertContains(response, 'NEW LEARNER CREDENTIALS')
        self.assertContains(response, 'CONFIDENTIAL: hand out, then destroy')
        self.assertContains(response, 'crest-logo.png')
        learner = CustomUser.objects.get(username='okafora')
        self.assertEqual(learner.user_type, '2')
        self.assertEqual(learner.student.school_class, self.school_class)
        self.assertEqual(learner.student.academic_period, self.period)
        self.assertTrue(learner.student.teacher_requests.filter(
            teacher=self.teacher_user.teacher,
            status=LearnerTeacherRequest.PENDING,
        ).exists())
        self.assertIsNone(learner.student.created_by)
        password = response.context['credentials'][0]['password']
        self.assertTrue(learner.check_password(password))
        self.assertTrue(ActivityLog.objects.filter(action='import').exists())
        self.client.post(reverse(
            'complete_learner_import',
            kwargs={'token': batch.token},
        ))
        batch.refresh_from_db()
        self.assertIsNotNone(batch.credentials_acknowledged_at)
        self.assertEqual(batch.rows, [])

    def test_teacher_import_assigns_accounts_only_to_themselves(self):
        self.client.force_login(self.teacher_user)
        response = self._preview_csv([[
            'Kai', 'Learner', 'M', 'Grade 7',
        ]])
        batch = response.context['preview_batch']
        response = self._confirm_import(batch)
        batch.refresh_from_db()
        self._finish_import(batch)

        learner = CustomUser.objects.get(username='learnerk')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(learner.student.teacher_requests.filter(
            teacher=self.teacher_user.teacher,
            status=LearnerTeacherRequest.ACCEPTED,
        ).exists())
        self.assertEqual(learner.student.created_by, self.teacher_user.teacher)

    def test_invalid_duplicate_and_class_rows_cannot_be_imported(self):
        response = self._preview_csv([
            ['Ada', 'Okafor', 'F', 'Grade 7'],
            ['Bea', 'Bello', 'F', '13A'],
        ])

        self.assertTrue(response.context['preview_has_errors'])
        self.assertContains(response, 'grade from 1 to 12')
        batch = response.context['preview_batch']
        response = self._confirm_import(batch)
        self.assertEqual(response.status_code, 200)
        batch.refresh_from_db()
        self.assertEqual(batch.status, LearnerImportBatch.PREVIEW)
        self.assertFalse(CustomUser.objects.filter(
            username__in=('okafora', 'bellob')).exists())
        self.assertContains(response, 'No accounts will be created')

    @override_settings(SIMS_LEARNER_IMPORT_MAX_ROWS=1)
    def test_import_row_limit_is_enforced(self):
        response = self._preview_csv([
            ['Ada', 'Okafor', 'F', 'Grade 7'],
            ['Bea', 'Bello', 'F', 'Grade 7'],
        ])
        self.assertContains(response, 'exceeds the 1-row limit')
        self.assertFalse(response.context['preview_rows'])

    def test_xlsx_import_and_template_are_supported(self):
        from openpyxl import Workbook, load_workbook

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append([
            'first_name', 'last_name', 'gender', 'class',
        ])
        worksheet.append(['Sam', 'Learner', 'M', 'Grade 7'])
        buffer = BytesIO()
        workbook.save(buffer)
        upload = SimpleUploadedFile(
            'learners.xlsx',
            buffer.getvalue(),
            content_type=(
                'application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet'),
        )
        response = self.client.post(reverse('import_learners'), {
            'action': 'upload',
            'file': upload,
        })
        self.assertContains(response, 'learners')

        template = self.client.get(
            reverse('download_learner_import_template'),
            {'format': 'xlsx'},
        )
        self.assertEqual(template.status_code, 200)
        template_book = load_workbook(BytesIO(template.content), read_only=True)
        self.assertEqual(
            list(next(template_book.active.iter_rows(values_only=True))),
            ['first_name', 'last_name', 'gender', 'class'],
        )
        template_book.close()

    def test_import_preview_expires_and_learner_role_is_denied(self):
        response = self._preview_csv([[
            'Ada', 'Okafor', 'F', 'Grade 7',
        ]])
        batch = response.context['preview_batch']
        LearnerImportBatch.objects.filter(pk=batch.pk).update(
            created_at=timezone.now() - timedelta(seconds=901))
        response = self._confirm_import(batch)
        self.assertRedirects(
            response, reverse('import_learners'),
            fetch_redirect_response=False)
        self.assertFalse(CustomUser.objects.filter(
            username='okafora').exists())

        learner = CustomUser.objects.create_user(
            username='import-denied',
            password='valid-password',
            user_type='2',
            gender='F',
        )
        self.client.force_login(learner)
        response = self.client.get(reverse('import_learners'))
        self.assertRedirects(
            response, reverse('student_home'),
            fetch_redirect_response=False)

    def test_interrupted_legacy_preview_is_recovered_from_session(self):
        response = self._preview_csv([[
            'Ada', 'Okafor', 'F', 'Grade 7',
        ]])
        rows = response.context['preview_batch'].rows
        LearnerImportBatch.objects.all().delete()
        session = self.client.session
        session['learner_import_preview'] = {
            'created_at': timezone.now().timestamp(),
            'rows': rows,
        }
        session.save()

        response = self.client.get(reverse('import_learners'))

        recovered = response.context['preview_batch']
        self.assertEqual(recovered.total_rows, 1)
        self.assertContains(response, 'Recovered the interrupted preview')
        self.assertNotIn('learner_import_preview', self.client.session)
        self.assertFalse(CustomUser.objects.filter(
            username='okafora').exists())

    @patch('main_app.hod_views.LEARNER_IMPORT_CHUNK_SIZE', 2)
    def test_import_progress_is_saved_and_resumes_without_duplicates(self):
        response = self._preview_csv([
            ['Ada', 'Okafor', 'F', 'Grade 7'],
            ['Bea', 'Bello', 'F', 'Grade 7'],
            ['Kai', 'Learner', 'M', 'Grade 7'],
        ])
        batch = response.context['preview_batch']
        self._confirm_import(batch)
        batch.refresh_from_db()

        process_url = reverse(
            'process_learner_import',
            kwargs={'token': batch.token},
        )
        response = self.client.post(process_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['recorded'], 2)
        batch.refresh_from_db()
        self.assertEqual(batch.imported_rows, 2)
        self.assertEqual(
            CustomUser.objects.filter(
                username__in=('okafora', 'bellob', 'learnerk'),
                user_type='2',
            ).count(),
            2,
        )

        response = self.client.get(reverse(
            'learner_import_status',
            kwargs={'token': batch.token},
        ))
        self.assertEqual(response.json()['recorded'], 2)
        self._finish_import(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.imported_rows, 3)
        self.assertEqual(batch.status, LearnerImportBatch.COMPLETED)
        self.assertEqual(
            CustomUser.objects.filter(
                username__in=('okafora', 'bellob', 'learnerk'),
                user_type='2',
            ).count(),
            3,
        )

    def test_500_row_import_reports_and_records_the_exact_progress(self):
        response = self._preview_csv([
            [f'Learner{index}', 'Bulk', 'M', 'Grade 7']
            for index in range(500)
        ])
        batch = response.context['preview_batch']
        self.assertEqual(batch.total_rows, 500)
        self._confirm_import(batch)
        batch.refresh_from_db()

        first_response = self.client.post(reverse(
            'process_learner_import',
            kwargs={'token': batch.token},
        ))

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.json()['recorded'], 10)
        self._finish_import(batch)
        batch.refresh_from_db()

        self.assertEqual(batch.status, LearnerImportBatch.COMPLETED)
        self.assertEqual(batch.imported_rows, 500)
        self.assertEqual(CustomUser.objects.filter(
            user_type='2',
            last_name='Bulk',
        ).count(), 500)

    def test_learner_csv_export_sanitizes_formulas_and_logs_the_export(self):
        learner = CustomUser.objects.create_user(
            username='csv-learner',
            password='valid-password',
            user_type='2',
            gender='F',
            first_name='=1+1',
        )
        learner.student.school_class = self.school_class
        learner.student.academic_period = self.period
        learner.student.save()

        response = self.client.get(reverse('manage_student'), {'export': 'csv'})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'\xef\xbb\xbf'))
        self.assertIn(b"'=1+1", response.content)
        self.assertIn('learner-list-term-1-2026.csv', response['Content-Disposition'])
        self.assertTrue(ActivityLog.objects.filter(
            action='export', actor=self.admin,
            description__contains='Learner List').exists())

    def test_teacher_list_and_login_activity_support_pdf_and_csv(self):
        teacher_csv = self.client.get(
            reverse('manage_teachers'), {'export': 'csv'})
        self.assertEqual(teacher_csv['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn(b'export-teacher', teacher_csv.content)
        teacher_pdf = self.client.get(
            reverse('manage_teachers'), {'export': 'pdf'})
        self.assertTrue(teacher_pdf.content.startswith(b'%PDF'))

        learner = CustomUser.objects.create_user(
            username='login-export-learner',
            password='valid-password',
            user_type='2',
            gender='M',
        )
        learner.student.school_class = self.school_class
        learner.student.save()
        LoginActivity.objects.create(student=learner.student)
        login_csv = self.client.get(
            reverse('learner_login_activity'), {'export': 'csv'},
            follow=True)
        self.assertEqual(login_csv.status_code, 200)
        self.assertIn(b'login-export-learner', login_csv.content)
        login_pdf = self.client.get(
            reverse('learner_login_activity'), {'export': 'pdf'},
            follow=True)
        self.assertTrue(login_pdf.content.startswith(b'%PDF'))

        learner_pdf = self.client.get(
            reverse('manage_student'), {'export': 'pdf'})
        self.assertTrue(learner_pdf.content.startswith(b'%PDF'))

    def test_activity_log_report_and_report_cards_export_as_csv(self):
        ActivityLog.objects.create(
            actor=self.admin,
            actor_username=self.admin.username,
            action='sign_in',
            description='Export test activity',
        )
        activity_csv = self.client.get(
            reverse('activity_log'), {'export': 'csv'})
        self.assertEqual(activity_csv.status_code, 200)
        self.assertIn(b'Export test activity', activity_csv.content)
        activity_pdf = self.client.get(
            reverse('activity_log'), {'export': 'pdf'})
        self.assertTrue(activity_pdf.content.startswith(b'%PDF'))

        learner = CustomUser.objects.create_user(
            username='report-export-learner',
            password='valid-password',
            user_type='2',
            gender='F',
            first_name='Report',
            last_name='Learner',
        )
        learner.student.school_class = self.school_class
        learner.student.academic_period = self.period
        learner.student.save()
        accept_learner_for_teacher(
            learner.student, self.teacher_user.teacher)
        subject = Subject.objects.create(
            name='Mathematics',
            school_class=self.school_class,
            teacher=self.teacher_user.teacher,
        )
        StudentResult.objects.create(
            student=learner.student, subject=subject, test=20, exam=70)
        assignments_csv = self.client.get(
            reverse('manage_assignments'), {'export': 'csv'})
        self.assertIn(b'export-teacher', assignments_csv.content)
        assignments_pdf = self.client.get(
            reverse('manage_assignments'), {'export': 'pdf'})
        self.assertTrue(assignments_pdf.content.startswith(b'%PDF'))

        accepted_learners_csv = self.client.get(
            reverse('assignment_learners'), {
                'teacher': self.teacher_user.teacher.pk,
                'school_class': self.school_class.pk,
                'export': 'csv',
            })
        self.assertIn(b'report-export-learner', accepted_learners_csv.content)
        accepted_learners_pdf = self.client.get(
            reverse('assignment_learners'), {
                'teacher': self.teacher_user.teacher.pk,
                'school_class': self.school_class.pk,
                'export': 'pdf',
            })
        self.assertTrue(accepted_learners_pdf.content.startswith(b'%PDF'))

        class_list_csv = self.client.post(reverse('generate_report'), {
            'report_type': 'class_list',
            'school_class': self.school_class.pk,
            'academic_period': self.period.pk,
            'export_format': 'csv',
        })
        self.assertIn(b'Class teacher', class_list_csv.content)
        self.assertIn(b'Not specified', class_list_csv.content)

        self.client.force_login(self.teacher_user)
        results_csv = self.client.post(reverse('generate_report'), {
            'report_type': 'learner_results',
            'school_class': self.school_class.pk,
            'academic_period': self.period.pk,
            'subject': subject.pk,
            'export_format': 'csv',
        })
        self.assertEqual(results_csv.status_code, 200)
        self.assertIn(b'Test 1 (50%)', results_csv.content)
        self.assertIn(b'Report Learner', results_csv.content)

        self.client.force_login(self.admin)
        report_csv = self.client.post(reverse('generate_report'), {
            'report_type': 'school_summary',
            'export_format': 'csv',
        })
        self.assertEqual(report_csv.status_code, 200)
        self.assertIn(b'School totals', report_csv.content)
        self.assertIn(b'Learners', report_csv.content)

        self.client.force_login(self.teacher_user)
        report_card_csv = self.client.post(
            reverse('generate_report_card'), {
                'student': learner.student.pk,
                'academic_period': self.period.pk,
                'export_format': 'csv',
            })
        self.assertEqual(report_card_csv.status_code, 200)
        self.assertIn(b'Mathematics', report_card_csv.content)

        self.client.force_login(learner)
        own_card_csv = self.client.get(
            reverse('student_report_card'), {'export': 'csv'})
        self.assertEqual(own_card_csv.status_code, 200)
        self.assertIn(b'Mathematics', own_card_csv.content)

    def test_empty_exports_return_page_message_without_a_file(self):
        response = self.client.get(
            reverse('activity_log'), {
                'search': 'no-such-activity-entry',
                'export': 'csv',
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/html; charset=utf-8')
        self.assertContains(response, 'No activity found for these filters.')

    def test_staff_forms_default_to_the_current_academic_period(self):
        from .forms import (
            ReportCardForm,
            ReportGenerationForm,
            StudentForm,
        )

        self.assertEqual(
            StudentForm().fields['academic_period'].initial, self.period)
        self.assertEqual(
            ReportGenerationForm().fields['academic_period'].initial,
            self.period,
        )
        self.assertEqual(
            ReportCardForm(user=self.teacher_user).fields[
                'academic_period'].initial,
            self.period,
        )

    def test_admin_can_change_current_period_and_used_period_cannot_be_deleted(self):
        next_period = AcademicPeriod.objects.create(year=2026, term=2)
        response = self.client.post(reverse(
            'set_current_academic_period', args=[next_period.pk]))
        self.assertRedirects(
            response, reverse('manage_academic_periods'),
            fetch_redirect_response=False)
        self.period.refresh_from_db()
        next_period.refresh_from_db()
        self.assertFalse(self.period.is_current)
        self.assertTrue(next_period.is_current)

        learner = CustomUser.objects.create_user(
            username='period-delete-learner',
            password='valid-password',
            user_type='2',
            gender='F',
        )
        learner.student.academic_period = next_period
        learner.student.save()
        subject = Subject.objects.create(name='Period subject')
        StudentResult.objects.create(
            student=learner.student,
            subject=subject,
            test=10,
            exam=20,
        )
        response = self.client.post(reverse(
            'delete_academic_period', args=[next_period.pk]))
        self.assertRedirects(
            response, reverse('manage_academic_periods'),
            fetch_redirect_response=False)
        self.assertTrue(AcademicPeriod.objects.filter(pk=next_period.pk).exists())
        self.assertContains(
            self.client.get(reverse('manage_academic_periods')),
            'cannot be deleted',
        )

    def test_xlsx_formula_cells_and_oversized_uploads_are_rejected(self):
        from openpyxl import Workbook

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append([
            'first_name', 'last_name', 'gender', 'class',
        ])
        worksheet.append(['=1+1', 'Learner', 'M', 'Grade 7'])
        buffer = BytesIO()
        workbook.save(buffer)
        formula_file = SimpleUploadedFile(
            'formula.xlsx', buffer.getvalue())
        response = self.client.post(reverse('import_learners'), {
            'action': 'upload',
            'file': formula_file,
        })
        self.assertContains(response, 'Formula cells are not allowed')
        self.assertFalse(response.context['preview_rows'])

        with override_settings(SIMS_LEARNER_IMPORT_MAX_BYTES=4):
            oversized = SimpleUploadedFile(
                'too-large.csv',
                b'first_name,last_name,gender,class',
            )
            response = self.client.post(reverse('import_learners'), {
                'action': 'upload',
                'file': oversized,
            })
        self.assertContains(response, 'exceeds the')
