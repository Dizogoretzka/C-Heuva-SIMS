from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .credentials import issue_pin
from .models import ActivityLog, CustomUser, SchoolClass
from .security_questions import (
    SECURITY_QUESTIONS,
    normalize_security_answer,
    placeholder_security_question,
)
from .security_reset import RESET_SESSION_KEY


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    SIMS_LOGIN_IP_MAX_FAILURES=20,
)
class SecurityQuestionProfileTests(TestCase):
    def setUp(self):
        cache.clear()
        self.school_class = SchoolClass.objects.create(
            grade_number=8,
            class_letter='A',
        )
        self.admin, self.admin_pin = self._create_staff(
            'security-admin', '1', 'Amina', 'Administrator')
        self.teacher, self.teacher_pin = self._create_staff(
            'security-teacher', '3', 'Tariq', 'Teacher')
        self.learner = CustomUser.objects.create_user(
            username='security-learner',
            password='LearnerInitialPassw0rd!',
            user_type='2',
            gender='F',
            first_name='Lina',
            last_name='Learner',
        )
        self.learner.student.school_class = self.school_class
        self.learner.student.save(update_fields=['school_class'])
        self.current_secrets = {
            self.admin.pk: ('current_pin', self.admin_pin),
            self.teacher.pk: ('current_pin', self.teacher_pin),
            self.learner.pk: (
                'current_password', 'LearnerInitialPassw0rd!'),
        }

    def _create_staff(self, username, user_type, first_name, last_name):
        user = CustomUser.objects.create_user(
            username=username,
            password='InitialStaffPassword123!',
            user_type=user_type,
            gender='M',
            first_name=first_name,
            last_name=last_name,
        )
        return user, issue_pin(user, '83472')

    @staticmethod
    def _role(user):
        return {'1': 'admin', '2': 'learner', '3': 'teacher'}[
            user.user_type]

    @staticmethod
    def _profile_url(user):
        return reverse({
            '1': 'admin_view_profile',
            '2': 'student_view_profile',
            '3': 'teacher_profile',
        }[user.user_type])

    def _set_question(self, user, answer='first school answer'):
        user.security_question = SECURITY_QUESTIONS[0]
        user.security_answer_hash = make_password(
            normalize_security_answer(answer))
        user.save(update_fields=[
            'security_question',
            'security_answer_hash',
        ])

    def _lookup_data(self, user, **overrides):
        data = {
            'first_name': user.first_name,
            'last_name': user.last_name,
        }
        if user.user_type == '2':
            data['grade_and_class'] = '8A'
        else:
            data['account_id'] = user.username
        data.update(overrides)
        return data

    def _start_reset(self, client, user, **overrides):
        role = self._role(user)
        data = {'action': 'lookup'}
        data.update(self._lookup_data(user, **overrides))
        return client.post(
            reverse('security_reset', args=[role]),
            data,
        )

    def _submit_answer(self, client, user, answer):
        return client.post(
            reverse('security_reset', args=[self._role(user)]),
            {'action': 'answer', 'answer': answer},
        )

    def _answer_correctly(self, client, user):
        response = self._submit_answer(
            client, user, '  FIRST   SCHOOL answer ')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['stage'], 'new_secret')
        return response.context['reset_token']

    def test_questions_are_hashed_and_never_exposed_in_pages_or_exports(self):
        answer = '  Private   RÉSPONSE never store 83  '
        normalized_answer = normalize_security_answer(answer)
        users = (self.admin, self.teacher, self.learner)

        for user in users:
            with self.subTest(role=user.user_type):
                field, current_secret = self.current_secrets[user.pk]
                self.client.force_login(user)
                response = self.client.post(
                    self._profile_url(user),
                    {
                        'action': 'security_question',
                        'question_choice': SECURITY_QUESTIONS[0],
                        'answer': answer,
                        field: current_secret,
                    },
                )
                self.assertEqual(response.status_code, 302)
                user.refresh_from_db()
                self.assertTrue(check_password(
                    normalized_answer, user.security_answer_hash))
                self.assertNotEqual(
                    user.security_answer_hash, normalized_answer)
                self.assertNotIn(answer.strip(), user.security_answer_hash)

                profile = self.client.get(self._profile_url(user))
                self.assertContains(profile, SECURITY_QUESTIONS[0])
                self.assertNotContains(profile, answer.strip())
                self.client.logout()

        activities = list(ActivityLog.objects.values(
            'description', 'changes'))
        activity_text = str(activities)
        self.assertNotIn(answer.strip(), activity_text)
        self.assertNotIn(normalized_answer, activity_text)

        self.client.force_login(self.admin)
        csv_response = self.client.get(
            reverse('activity_log'), {'export': 'csv'})
        pdf_response = self.client.get(
            reverse('activity_log'), {'export': 'pdf'})
        self.assertEqual(csv_response.status_code, 200)
        self.assertEqual(pdf_response.status_code, 200)
        self.assertNotIn(answer.strip().encode(), csv_response.content)
        self.assertNotIn(answer.strip().encode(), pdf_response.content)

    def test_answer_minimum_and_current_secret_are_required_to_change_question(self):
        self.client.force_login(self.learner)
        profile_url = self._profile_url(self.learner)
        short_answer = self.client.post(profile_url, {
            'action': 'security_question',
            'question_choice': SECURITY_QUESTIONS[0],
            'answer': ' a  é ',
            'current_password': 'LearnerInitialPassw0rd!',
        })
        self.assertEqual(short_answer.status_code, 200)
        self.assertIn(
            'The answer must contain at least 4 characters.',
            short_answer.context['security_question_form'].errors['answer'],
        )
        self.learner.refresh_from_db()
        self.assertFalse(self.learner.security_question)

        self._set_question(self.learner)
        rejected = self.client.post(profile_url, {
            'action': 'security_question',
            'question_choice': SECURITY_QUESTIONS[1],
            'answer': 'A different answer',
            'current_password': 'incorrect password',
        })
        self.assertEqual(rejected.status_code, 200)
        self.learner.refresh_from_db()
        self.assertEqual(
            self.learner.security_question, SECURITY_QUESTIONS[0])
        self.assertIn(
            'current_password',
            rejected.context['security_question_form'].errors,
        )

        for user in (self.admin, self.teacher):
            with self.subTest(role=user.user_type):
                self._set_question(user)
                self.client.force_login(user)
                rejected = self.client.post(self._profile_url(user), {
                    'action': 'security_question',
                    'question_choice': SECURITY_QUESTIONS[1],
                    'answer': 'A different answer',
                    'current_pin': 'wrong',
                })
                self.assertEqual(rejected.status_code, 200)
                user.refresh_from_db()
                self.assertEqual(
                    user.security_question, SECURITY_QUESTIONS[0])
                self.assertIn(
                    'current_pin',
                    rejected.context['security_question_form'].errors,
                )

    def test_reset_flow_for_each_role_shows_pin_once_and_notice_once(self):
        new_password = 'V3ry$trong-unique-682!'
        for user in (self.admin, self.teacher, self.learner):
            with self.subTest(role=user.user_type):
                self._set_question(user)
                user.failed_login_attempts = 3
                user.locked_until = timezone.now() + timedelta(minutes=20)
                user.security_reset_failed_attempts = 2
                if user is self.learner:
                    user.is_imported_learner = True
                    user.password_setup_required = True
                user.save(update_fields=[
                    'failed_login_attempts',
                    'locked_until',
                    'security_reset_failed_attempts',
                    'is_imported_learner',
                    'password_setup_required',
                ])

                client = Client()
                start = self._start_reset(client, user)
                self.assertEqual(start.status_code, 200)
                self.assertEqual(
                    start.context['question'], SECURITY_QUESTIONS[0])
                token = self._answer_correctly(client, user)

                role = self._role(user)
                reset_data = {'action': 'reset', 'reset_token': token}
                if role == 'learner':
                    reset_data.update({
                        'new_password1': new_password,
                        'new_password2': new_password,
                    })
                result = client.post(
                    reverse('security_reset', args=[role]),
                    reset_data,
                )
                self.assertEqual(result.status_code, 200)
                self.assertContains(
                    result,
                    'Your password or PIN was reset. Sign in with your new secret.',
                )
                user.refresh_from_db()
                self.assertEqual(user.failed_login_attempts, 0)
                self.assertIsNone(user.locked_until)
                self.assertEqual(user.security_reset_failed_attempts, 0)
                self.assertIsNotNone(user.security_reset_at)
                self.assertTrue(user.security_reset_notice_pending)
                if role == 'learner':
                    self.assertFalse(user.password_setup_required)
                    self.assertTrue(user.check_password(new_password))
                    login_data = {
                        'role': role,
                        'account_id': user.username,
                        'password': new_password,
                    }
                else:
                    new_pin = result.context['reset_pin_once']
                    self.assertTrue(
                        result.content.count(new_pin.encode()) >= 1)
                    self.assertTrue(check_password(new_pin, user.pin_hash))
                    self.assertNotIn(new_pin.encode(), client.get(
                        reverse('role_login', args=[role])).content)
                    login_data = {
                        'role': role,
                        'account_id': user.username,
                        'pin': new_pin,
                    }
                self.assertTrue(ActivityLog.objects.filter(
                    action='security_reset',
                    object_type='self_service_reset_completed',
                    target_user=user,
                ).exists())

                signed_in = client.post(
                    reverse('user_login'),
                    login_data,
                    follow=True,
                )
                self.assertContains(
                    signed_in,
                    'using your security question. If this was not you, tell your administrator.',
                )
                user.refresh_from_db()
                self.assertFalse(user.security_reset_notice_pending)
                next_page = {
                    '1': 'admin_home',
                    '2': 'student_home',
                    '3': 'teacher_home',
                }[user.user_type]
                self.assertNotContains(
                    client.get(reverse(next_page)),
                    'using your security question. If this was not you',
                )

    def test_wrong_answers_block_account_for_one_hour(self):
        self._set_question(self.learner)
        client = Client()
        self._start_reset(client, self.learner)

        for _ in range(5):
            response = self._submit_answer(
                client, self.learner, 'wrong answer')
            self.assertContains(response, 'The details did not match.')

        self.learner.refresh_from_db()
        self.assertEqual(self.learner.security_reset_failed_attempts, 5)
        self.assertAlmostEqual(
            (
                self.learner.security_reset_locked_until
                - timezone.now()
            ).total_seconds(),
            60 * 60,
            delta=5,
        )
        blocked = self._submit_answer(
            client, self.learner, 'first school answer')
        self.assertContains(blocked, 'The details did not match.')
        self.assertTrue(ActivityLog.objects.filter(
            action='security_reset',
            description__contains='blocked for one hour',
            target_user=self.learner,
        ).exists())

    def test_unknown_and_unconfigured_accounts_use_same_failure_and_hash_cost(self):
        self._set_question(self.learner)
        real_client = Client()
        self._start_reset(real_client, self.learner)
        with patch(
            'main_app.security_reset.check_password',
            wraps=check_password,
        ) as password_check:
            real_response = self._submit_answer(
                real_client, self.learner, 'wrong answer')
            self.assertEqual(password_check.call_count, 1)

        unknown_client = Client()
        unknown_response = unknown_client.post(
            reverse('security_reset', args=['learner']),
            {
                'action': 'lookup',
                'first_name': 'Unknown',
                'last_name': 'Learner',
                'grade_and_class': '8A',
            },
        )
        self.assertEqual(
            unknown_response.context['question'],
            placeholder_security_question('Unknown', 'Learner', 'learner'),
        )
        with patch(
            'main_app.security_reset.check_password',
            wraps=check_password,
        ) as password_check:
            unknown_response = unknown_client.post(
                reverse('security_reset', args=['learner']),
                {'action': 'answer', 'answer': 'wrong answer'},
            )
            self.assertEqual(password_check.call_count, 1)

        generic_message = 'The details did not match.'
        self.assertContains(real_response, generic_message)
        self.assertContains(unknown_response, generic_message)
        self.assertEqual(
            real_response.context['generic_error'],
            unknown_response.context['generic_error'],
        )

        self.learner.security_question = ''
        self.learner.security_answer_hash = ''
        self.learner.security_reset_failed_attempts = 0
        self.learner.save(update_fields=[
            'security_question',
            'security_answer_hash',
            'security_reset_failed_attempts',
        ])
        no_question_client = Client()
        no_question_response = self._start_reset(
            no_question_client, self.learner)
        self.assertEqual(
            no_question_response.context['question'],
            placeholder_security_question(
                self.learner.first_name,
                self.learner.last_name,
                'learner',
            ),
        )
        failed = self._submit_answer(
            no_question_client, self.learner, 'any answer')
        self.assertContains(failed, generic_message)
        self.learner.refresh_from_db()
        self.assertEqual(self.learner.security_reset_failed_attempts, 1)

    def test_duplicate_learners_are_disambiguated_by_account_id(self):
        first = CustomUser.objects.create_user(
            username='duplicate-learner-a',
            password='DuplicateInitialPassword!',
            user_type='2',
            gender='F',
            first_name='Áva',
            last_name='Dôe',
        )
        second = CustomUser.objects.create_user(
            username='duplicate-learner-b',
            password='DuplicateInitialPassword!',
            user_type='2',
            gender='F',
            first_name='Ava',
            last_name='Doe',
        )
        for user in (first, second):
            user.student.school_class = self.school_class
            user.student.save(update_fields=['school_class'])
        self._set_question(first, 'first learner answer')
        self._set_question(second, 'second learner answer')

        client = Client()
        response = client.post(
            reverse('security_reset', args=['learner']),
            {
                'action': 'lookup',
                'first_name': ' ava ',
                'last_name': 'D O E',
                'grade_and_class': '8 a',
            },
        )
        self.assertTrue(response.context['account_id_required'])
        self.assertEqual(
            list(response.context['lookup_form'].fields),
            ['account_id'],
        )
        response = client.post(
            reverse('security_reset', args=['learner']),
            {'action': 'lookup', 'account_id': first.username},
        )
        self.assertEqual(
            response.context['question'], SECURITY_QUESTIONS[0])

        wrong_id_client = Client()
        wrong_id_client.post(
            reverse('security_reset', args=['learner']),
            {
                'action': 'lookup',
                'first_name': 'Ava',
                'last_name': 'Doe',
                'grade_and_class': '8A',
            },
        )
        ambiguous = wrong_id_client.post(
            reverse('security_reset', args=['learner']),
            {'action': 'lookup', 'account_id': 'not-an-account'},
        )
        self.assertContains(
            ambiguous,
            'We cannot reset this account online. Ask your teacher or administrator.',
        )

    def test_reset_step_three_requires_an_unexpired_server_token(self):
        self._set_question(self.learner)
        client = Client()
        url = reverse('security_reset', args=['learner'])
        direct = client.get(url)
        self.assertEqual(direct.context['stage'], 'lookup')
        rejected = client.post(url, {
            'action': 'reset',
            'reset_token': 'missing-token',
            'new_password1': 'V3ry$trong-unique-682!',
            'new_password2': 'V3ry$trong-unique-682!',
        })
        self.assertEqual(rejected.status_code, 302)
        self.learner.refresh_from_db()
        self.assertTrue(self.learner.check_password(
            'LearnerInitialPassw0rd!'))

        self._start_reset(client, self.learner)
        token = self._answer_correctly(client, self.learner)
        session = client.session
        state = session[RESET_SESSION_KEY]
        state['expires_at'] = timezone.now().timestamp() - 1
        session[RESET_SESSION_KEY] = state
        session.save()
        expired = client.post(url, {
            'action': 'reset',
            'reset_token': token,
            'new_password1': 'V3ry$trong-unique-682!',
            'new_password2': 'V3ry$trong-unique-682!',
        })
        self.assertEqual(expired.status_code, 302)
        self.learner.refresh_from_db()
        self.assertTrue(self.learner.check_password(
            'LearnerInitialPassw0rd!'))

    def test_reset_revokes_other_sessions_and_clears_activation_and_lockout(self):
        self._set_question(self.learner)
        self.learner.is_imported_learner = True
        self.learner.password_setup_required = True
        self.learner.failed_login_attempts = 4
        self.learner.locked_until = timezone.now() + timedelta(minutes=10)
        self.learner.save(update_fields=[
            'is_imported_learner',
            'password_setup_required',
            'failed_login_attempts',
            'locked_until',
        ])
        other_client = Client()
        other_client.force_login(self.learner)
        self.assertTrue(Session.objects.filter(
            session_key=other_client.session.session_key).exists())

        client = Client()
        self._start_reset(client, self.learner)
        token = self._answer_correctly(client, self.learner)
        client.post(reverse('security_reset', args=['learner']), {
            'action': 'reset',
            'reset_token': token,
            'new_password1': 'V3ry$trong-unique-682!',
            'new_password2': 'V3ry$trong-unique-682!',
        })

        self.learner.refresh_from_db()
        self.assertFalse(self.learner.password_setup_required)
        self.assertEqual(self.learner.failed_login_attempts, 0)
        self.assertIsNone(self.learner.locked_until)
        self.assertFalse(Session.objects.filter(
            session_key=other_client.session.session_key).exists())
        self.assertEqual(
            other_client.get(reverse('student_home')).status_code, 302)

    @override_settings(SIMS_LOGIN_IP_MAX_FAILURES=2)
    def test_per_ip_limit_uses_a_hashed_cache_key(self):
        self._set_question(self.learner)
        client = Client()
        self._start_reset(client, self.learner)
        self._submit_answer(client, self.learner, 'wrong answer')
        self._submit_answer(client, self.learner, 'wrong answer')
        blocked = self._submit_answer(client, self.learner, 'wrong answer')
        self.assertContains(blocked, 'The details did not match.')
        self.assertTrue(ActivityLog.objects.filter(
            action='security_reset',
            description__contains='per-IP attempt limit',
        ).exists())

        from .security_reset import _reset_ip_cache_key
        request = type('Request', (), {'META': {
            'REMOTE_ADDR': '127.0.0.1',
        }})()
        key = _reset_ip_cache_key(request)
        self.assertNotIn('127.0.0.1', key)
        self.assertTrue(cache.get(key))

    def test_superuser_is_not_a_reset_target(self):
        root = CustomUser.objects.create_superuser(
            username='security-root',
            password='RootInitialPassword!',
            user_type='1',
            gender='M',
            first_name='Root',
            last_name='Administrator',
        )
        self._set_question(root, 'root security answer')
        client = Client()
        response = client.post(
            reverse('security_reset', args=['admin']),
            {
                'action': 'lookup',
                'first_name': root.first_name,
                'last_name': root.last_name,
                'account_id': root.username,
            },
        )
        self.assertEqual(response.context['stage'], 'answer')
        failed = client.post(
            reverse('security_reset', args=['admin']),
            {'action': 'answer', 'answer': 'root security answer'},
        )
        self.assertContains(failed, 'The details did not match.')
        root.refresh_from_db()
        self.assertTrue(root.check_password('RootInitialPassword!'))

        client.force_login(root)
        self.assertEqual(
            client.get(reverse('security_reset', args=['admin'])).status_code,
            404,
        )
