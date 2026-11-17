from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .activity import record_failed_attempt
from .models import ActivityLog, CustomUser, LoginActivity, SchoolClass


class ActivityIpRemovalTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = CustomUser.objects.create_user(
            username='activity-ip-admin',
            password='AdminInitialPassword!',
            user_type='1',
            gender='M',
            first_name='Activity',
            last_name='Administrator',
        )
        self.learner = CustomUser.objects.create_user(
            username='activity-ip-learner',
            password='LearnerInitialPassword!',
            user_type='2',
            gender='F',
            first_name='Activity',
            last_name='Learner',
        )
        self.learner.student.school_class = SchoolClass.objects.create(
            grade_number=8,
            class_letter='A',
        )
        self.learner.student.save(update_fields=['school_class'])

    def test_ip_columns_are_removed_from_both_activity_models(self):
        for model in (ActivityLog, LoginActivity):
            with self.subTest(model=model.__name__):
                self.assertNotIn(
                    'ip_address',
                    {field.name for field in model._meta.get_fields()},
                )
                with connection.cursor() as cursor:
                    columns = connection.introspection.get_table_description(
                        cursor, model._meta.db_table)
                self.assertNotIn(
                    'ip_address',
                    {column.name for column in columns},
                )

    def test_activity_page_timelines_and_exports_never_include_ip(self):
        remote_address = '203.0.113.88'
        record_failed_attempt(
            type('Request', (), {'META': {
                'REMOTE_ADDR': remote_address,
            }})(),
            self.learner.username,
        )
        LoginActivity.objects.create(student=self.learner.student)
        admin_client = Client()
        admin_client.force_login(self.admin)

        pages = (
            reverse('activity_log'),
            reverse('activity_user_timeline', args=[self.learner.pk]),
        )
        for path in pages:
            response = admin_client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'IP address')
            self.assertNotContains(response, remote_address)
            self.assertNotIn(
                'ip_address', response.context['filter_form'].fields)
            for export_format in ('csv', 'pdf'):
                export = admin_client.get(
                    path, {'export': export_format})
                self.assertEqual(export.status_code, 200)
                self.assertNotIn(b'IP address', export.content)
                self.assertNotIn(
                    remote_address.encode(), export.content)

        activity_data = str(list(ActivityLog.objects.values(
            'description', 'changes')))
        self.assertNotIn(remote_address, activity_data)

    @override_settings(SIMS_LOGIN_IP_MAX_FAILURES=1)
    def test_login_ip_limit_still_works_without_database_ip_storage(self):
        remote_address = '198.51.100.61'
        client = Client()
        failed = client.post(
            reverse('user_login'),
            {
                'role': 'learner',
                'account_id': self.learner.username,
                'password': 'wrong password',
            },
            REMOTE_ADDR=remote_address,
        )
        self.assertEqual(failed.status_code, 302)

        same_ip = client.post(
            reverse('user_login'),
            {
                'role': 'learner',
                'account_id': self.learner.username,
                'password': 'LearnerInitialPassword!',
            },
            REMOTE_ADDR=remote_address,
        )
        self.assertEqual(same_ip.status_code, 302)
        self.assertNotIn('_auth_user_id', client.session)

        different_ip = client.post(
            reverse('user_login'),
            {
                'role': 'learner',
                'account_id': self.learner.username,
                'password': 'LearnerInitialPassword!',
            },
            REMOTE_ADDR='198.51.100.62',
        )
        self.assertRedirects(
            different_ip,
            reverse('student_home'),
            fetch_redirect_response=False,
        )
        activity_data = str(list(ActivityLog.objects.values(
            'description', 'changes')))
        self.assertNotIn(remote_address, activity_data)
