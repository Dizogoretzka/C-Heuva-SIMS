from django.test import TestCase
from django.urls import Resolver404, resolve, reverse

from .models import (
    Attendance,
    AttendanceReport,
    Course,
    CustomUser,
    Session,
    Student,
    StudentResult,
    Subject,
)


class InterfaceSelectionTests(TestCase):
    def test_landing_page_offers_admin_and_learner_interfaces(self):
        response = self.client.get(reverse('login_page'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Admin')
        self.assertContains(response, 'Learner')
        self.assertContains(response, reverse('role_login', args=['admin']))
        self.assertContains(response, reverse('role_login', args=['learner']))

    def test_each_role_has_a_login_page(self):
        for role in ('admin', 'learner'):
            with self.subTest(role=role):
                response = self.client.get(reverse('role_login', args=[role]))

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f'Sign in as {role.title()}')

    def test_learner_can_sign_in_through_learner_interface(self):
        CustomUser.objects.create_user(
            email='learner@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )

        response = self.client.post(reverse('user_login'), {
            'email': 'learner@example.com',
            'password': 'valid-password',
            'role': 'learner',
        })

        self.assertRedirects(
            response,
            reverse('student_home'),
            fetch_redirect_response=False,
        )

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

    def test_learner_dashboard_loads_without_attendance_or_course_features(self):
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

    def test_learner_sees_only_subjects_assigned_to_their_course(self):
        learner = CustomUser.objects.create_user(
            email='subjects-learner@example.com',
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
        )
        learner_course = Course.objects.create(name='Grade 7')
        other_course = Course.objects.create(name='Grade 8')
        learner.student.course = learner_course
        learner.student.save()
        Subject.objects.create(name='Mathematics', course=learner_course)
        Subject.objects.create(name='History', course=other_course)
        self.client.force_login(learner)

        response = self.client.get(reverse('student_view_subjects'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Mathematics')
        self.assertNotContains(response, 'History')
        self.assertNotContains(response, 'Add Subject')

    def test_subject_added_by_admin_appears_on_learner_subject_page(self):
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
        add_response = self.client.post(reverse('add_subject'), {
            'name': 'Science',
        })
        self.assertRedirects(
            add_response, reverse('manage_subject'), fetch_redirect_response=False)

        self.client.force_login(learner)
        learner_response = self.client.get(reverse('student_view_subjects'))

        self.assertEqual(learner_response.status_code, 200)
        self.assertContains(learner_response, 'Science')

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
            'No subjects have been added by the administrator yet.',
        )

    def test_admin_dashboard_has_no_removed_feature_links(self):
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
        for label in (
            'Add Course',
            'Manage Course',
            'Add Session',
            'Manage Session',
            'View Attendance',
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
        self.client.force_login(admin)

        response = self.client.get(reverse('manage_student'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-target="#addStudentModal"')
        self.assertContains(response, 'action="' + reverse('add_student') + '"')
        self.assertNotContains(response, 'addSubjectModal')
        self.assertNotContains(response, 'Course / grade')
        self.assertNotContains(response, 'Session / term')
        self.assertNotContains(response, 'id_course')
        self.assertNotContains(response, 'id_session')
        self.assertContains(response, 'Add Learner')
        self.assertNotContains(response, 'Add Student')
        self.assertNotContains(response, 'Add Subject</p>')

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

    def test_add_pages_redirect_to_manage_students(self):
        admin = CustomUser.objects.create_user(
            email='add-subject-admin@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        self.client.force_login(admin)

        student_response = self.client.get(reverse('add_student'))
        subject_response = self.client.get(reverse('add_subject'))

        self.assertRedirects(
            student_response, reverse('manage_student'),
            fetch_redirect_response=False)
        self.assertRedirects(
            subject_response, reverse('manage_subject'),
            fetch_redirect_response=False)

    def test_manage_subjects_has_add_subject_modal(self):
        admin = CustomUser.objects.create_user(
            email='manage-subject-admin@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        self.client.force_login(admin)

        response = self.client.get(reverse('manage_subject'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-target="#addSubjectModal"')
        self.assertContains(response, 'action="' + reverse('add_subject') + '"')
        self.assertNotContains(response, 'id_course')

    def test_invalid_subject_submission_reopens_subject_modal(self):
        admin = CustomUser.objects.create_user(
            email='invalid-subject-admin@example.com',
            password='valid-password',
            user_type='1',
            gender='F',
            address='',
        )
        self.client.force_login(admin)

        response = self.client.post(reverse('add_subject'), {'name': ''})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "$('#addSubjectModal').modal('show')")
        self.assertContains(response, 'This field is required.')

    def test_generate_report_link_is_in_admin_sidebar_only(self):
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

        self.client.force_login(admin)
        admin_response = self.client.get(reverse('admin_home'))
        self.assertContains(admin_response, 'Generate Report')
        self.assertContains(admin_response, 'Manage Results')
        self.assertContains(
            admin_response, reverse('manage_student_results'), html=False)
        self.assertContains(
            admin_response, reverse('generate_report'), html=False)
        admin_html = admin_response.content.decode()
        self.assertLess(admin_html.index('Generate Report'), admin_html.index('Logout'))
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
            ('manage_subject', 'manage-subject-list'),
            ('manage_student_results', 'manage-results-list'),
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

    def test_removed_management_and_attendance_routes_are_not_registered(self):
        removed_paths = (
            '/course/add',
            '/course/manage/',
            '/add_session/',
            '/session/manage/',
            '/attendance/view/',
            '/attendance/fetch/',
            '/get_attendance',
            '/student/view/attendance/',
        )
        for path in removed_paths:
            with self.subTest(path=path):
                with self.assertRaises(Resolver404):
                    resolve(path)

    def test_student_and_subject_forms_support_report_class_filters(self):
        from .forms import StudentForm

        self.assertIn('course', StudentForm().fields)
        self.assertIn('session', StudentForm().fields)
        self.assertIn('course', [field.name for field in Subject._meta.fields])

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
            password='valid-password',
            user_type='2',
            gender='F',
            address='',
            first_name='Test',
            last_name='Learner',
        )
        course = Course.objects.create(name='Grade 7')
        session = Session.objects.create(
            start_year='2026-01-01', end_year='2026-12-31')
        subject = Subject.objects.create(name='Mathematics', course=course)
        learner.student.course = course
        learner.student.session = session
        learner.student.save()
        StudentResult.objects.create(
            student=learner.student, subject=subject, test=18, exam=72)
        attendance = Attendance.objects.create(
            session=session, subject=subject, date='2026-06-01')
        AttendanceReport.objects.create(
            student=learner.student, attendance=attendance, status=True)
        self.client.force_login(admin)

        report_options = [
            {'report_type': 'school_summary'},
            {
                'report_type': 'class_list',
                'course': course.pk,
                'session': session.pk,
            },
            {
                'report_type': 'learner_results',
                'course': course.pk,
                'session': session.pk,
                'subject': subject.pk,
            },
            {
                'report_type': 'attendance_summary',
                'course': course.pk,
                'session': session.pk,
            },
        ]
        for options in report_options:
            with self.subTest(report_type=options['report_type']):
                response = self.client.post(reverse('generate_report'), options)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Content-Type'], 'application/pdf')
                self.assertTrue(response.content.startswith(b'%PDF'))
                self.assertIn('attachment;', response['Content-Disposition'])

    def test_admin_can_add_and_update_result_and_learner_sees_latest_scores(self):
        admin = CustomUser.objects.create_user(
            email='result-manager@example.com',
            password='valid-password',
            user_type='1',
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
        subject = Subject.objects.create(name='Mathematics')
        self.client.force_login(admin)

        response = self.client.post(reverse('manage_student_results'), {
            'student': learner.student.pk,
            'subject': subject.pk,
            'test': '18',
            'exam': '72',
        })

        self.assertRedirects(
            response,
            reverse('manage_student_results'),
            fetch_redirect_response=False,
        )
        result = StudentResult.objects.get(
            student=learner.student, subject=subject)
        self.assertEqual(result.test, 18)
        self.assertEqual(result.exam, 72)

        response = self.client.post(
            reverse('edit_student_result', args=[result.pk]),
            {
                'student': learner.student.pk,
                'subject': subject.pk,
                'test': '20',
                'exam': '75',
            },
        )
        self.assertRedirects(
            response,
            reverse('manage_student_results'),
            fetch_redirect_response=False,
        )

        self.client.force_login(learner)
        response = self.client.get(reverse('student_view_result'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Mathematics')
        self.assertContains(response, '20.0')
        self.assertContains(response, '75.0')
        self.assertContains(response, '95.0')

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
        CustomUser.objects.create_user(
            email='admin@example.com',
            password='valid-password',
            user_type=1,
            gender='F',
            address='',
        )

        response = self.client.post(reverse('user_login'), {
            'email': 'admin@example.com',
            'password': 'valid-password',
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
            'email': 'admin@example.com',
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
            'email': 'learner@example.com',
            'password': 'incorrect-password',
            'role': 'learner',
        })

        self.assertRedirects(
            response,
            reverse('role_login', args=['learner']),
            fetch_redirect_response=False,
        )
        self.assertNotIn('_auth_user_id', self.client.session)
