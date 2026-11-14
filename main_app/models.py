from decimal import Decimal, InvalidOperation
import uuid

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import UserManager
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from django.db.models.signals import post_save
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.validators import RegexValidator
from django.db.models import (
    Q,
)
from django.db.models.functions import Lower
from django.core.validators import MaxValueValidator, MinValueValidator
from django.utils import timezone

from .account_ids import generate_account_id
from .class_parser import format_class_label, parse_class_label




class UsernameUserManager(UserManager):
    def _create_user(self, username=None, password=None, **extra_fields):
        if username:
            username = username.strip().lower()
        email = extra_fields.get('email')
        if email:
            extra_fields['email'] = self.normalize_email(email)
        user = self.model(username=username or None, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, username=None, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(username, password, **extra_fields)

    def create_superuser(self, username=None, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        if not extra_fields['is_staff'] or not extra_fields['is_superuser']:
            raise ValueError('Superusers must have staff and superuser status.')
        if not username:
            raise ValueError('Superusers must have a username.')
        return self._create_user(username, password, **extra_fields)


# The initial migration imports this historical manager name.
CustomUserManager = UsernameUserManager


class CustomUser(AbstractUser):
    USER_TYPE = ((1, "Admin"), (2, "Learner"), (3, "Teacher"))
    GENDER = [("M", "Male"), ("F", "Female")]
    
    
    username = models.CharField(
        verbose_name='account ID',
        max_length=160,
        unique=True,
        blank=True,
        null=True,
        validators=[
            RegexValidator(
                regex=r'^[\w][\w.-]{1,159}$',
                message='Account IDs must contain at least two characters.',
            ),
        ],
    )
    email = models.EmailField(blank=True, default='')
    user_type = models.CharField(default=1, choices=USER_TYPE, max_length=1)
    gender = models.CharField(max_length=1, choices=GENDER)
    profile_pic = models.ImageField(blank=True)
    address = models.TextField(blank=True, default='')
    fcm_token = models.TextField(default="")  # For firebase notifications
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    pin_hash = models.CharField(max_length=128, blank=True, default='')
    pin_issued_at = models.DateTimeField(null=True, blank=True)
    failed_login_attempts = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = []
    objects = UsernameUserManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower('username'),
                condition=Q(username__isnull=False),
                name='customuser_username_ci_uniq',
            ),
        ]

    def __str__(self):
        return self.last_name + ", " + self.first_name

    def save(self, *args, **kwargs):
        if not self.username and self.first_name and self.last_name:
            existing_ids = CustomUser.objects.exclude(
                pk=self.pk).values_list('username', flat=True)
            self.username = generate_account_id(
                self.first_name, self.last_name, existing_ids)
        super().save(*args, **kwargs)


class Admin(models.Model):
    admin = models.OneToOneField(CustomUser, on_delete=models.CASCADE)


class Teacher(models.Model):
    teacher = models.OneToOneField(CustomUser, on_delete=models.CASCADE)

    def __str__(self):
        return (
            self.teacher.get_full_name()
            or self.teacher.username
            or f'Teacher {self.pk}'
        )


class SchoolClass(models.Model):
    grade_number = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(12)],
    )
    class_letter = models.CharField(max_length=1, blank=True, default='')
    legacy_label = models.CharField(max_length=120, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['grade_number', 'class_letter'],
                condition=Q(grade_number__isnull=False),
                name='unique_school_class_grade_letter',
            ),
        ]
        ordering = ['grade_number', 'class_letter']

    def __str__(self):
        return format_class_label(self.grade_number, self.class_letter)


def ordered_school_classes():
    return SchoolClass.objects.filter(
        grade_number__isnull=False).order_by('grade_number', 'class_letter')


def school_class_from_label(value):
    parsed = parse_class_label(value)
    if parsed is None:
        return None
    grade_number, class_letter = parsed
    school_class, _ = SchoolClass.objects.get_or_create(
        grade_number=grade_number,
        class_letter=class_letter,
    )
    return school_class


class AcademicPeriod(models.Model):
    start_year = models.DateField(null=True, blank=True)
    end_year = models.DateField(null=True, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    term = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        choices=((1, 'Term 1'), (2, 'Term 2'), (3, 'Term 3')),
    )
    is_current = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['year', 'term'],
                condition=Q(year__isnull=False, term__isnull=False),
                name='unique_academic_period_year_term',
            ),
            models.UniqueConstraint(
                fields=['is_current'],
                condition=Q(is_current=True),
                name='only_one_current_academic_period',
            ),
        ]

    def clean(self):
        from django.core.exceptions import ValidationError

        if (self.year is None) != (self.term is None):
            raise ValidationError(
                'Choose both a year and a term, or leave both empty for a '
                'legacy academic period.')
        if self.term is not None and self.term not in (1, 2, 3):
            raise ValidationError({'term': 'Choose term 1, 2, or 3.'})

    def __str__(self):
        if self.year and self.term:
            return f'Term {self.term} {self.year}'
        if self.start_year and self.end_year:
            return f'From {self.start_year} to {self.end_year}'
        return f'Academic period {self.pk}'


class Student(models.Model):
    admin = models.OneToOneField(CustomUser, on_delete=models.CASCADE)
    created_by = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_learners',
    )
    school_class = models.ForeignKey(
        SchoolClass, on_delete=models.SET_NULL, null=True, blank=True)
    academic_period = models.ForeignKey(
        AcademicPeriod, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        verbose_name = 'learner'
        verbose_name_plural = 'learners'

    def __str__(self):
        return self.admin.last_name + ", " + self.admin.first_name


class LearnerImportBatch(models.Model):
    PREVIEW = 'preview'
    PROCESSING = 'processing'
    COMPLETED = 'completed'
    FAILED = 'failed'
    STATUS_CHOICES = (
        (PREVIEW, 'Preview'),
        (PROCESSING, 'Processing'),
        (COMPLETED, 'Completed'),
        (FAILED, 'Failed'),
    )

    token = models.UUIDField(
        default=uuid.uuid4, unique=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='learner_import_batches',
    )
    rows = models.JSONField(default=list)
    total_rows = models.PositiveIntegerField(default=0)
    imported_rows = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=PREVIEW)
    academic_period = models.ForeignKey(
        'AcademicPeriod', null=True, blank=True, on_delete=models.SET_NULL)
    teacher = models.ForeignKey(
        'Teacher', null=True, blank=True, on_delete=models.SET_NULL)
    error_message = models.TextField(blank=True, default='')
    credentials_acknowledged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-pk']


class ReportCardPublication(models.Model):
    teacher = models.ForeignKey(
        Teacher,
        on_delete=models.CASCADE,
        related_name='report_card_publications',
    )
    student = models.ForeignKey(
        Student,
        on_delete=models.CASCADE,
        related_name='report_card_publications',
    )
    academic_period = models.ForeignKey(
        AcademicPeriod,
        on_delete=models.CASCADE,
        related_name='report_card_publications',
    )
    published_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['teacher', 'student', 'academic_period'],
                name='unique_teacher_learner_period_report_card',
            ),
        ]
        ordering = ['-published_at', '-pk']


class TeacherClassSetup(models.Model):
    teacher = models.ForeignKey(
        Teacher, on_delete=models.CASCADE, related_name='class_setups')
    school_class = models.ForeignKey(
        SchoolClass, on_delete=models.CASCADE, related_name='teacher_setups')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['teacher', 'school_class'],
                name='unique_teacher_school_class_setup',
            ),
        ]
        ordering = ['school_class__grade_number', 'school_class__class_letter']

    def __str__(self):
        return f'{self.teacher} — {self.school_class}'


class LearnerTeacherRequest(models.Model):
    PENDING = 'pending'
    ACCEPTED = 'accepted'
    DECLINED = 'declined'
    CANCELLED = 'cancelled'
    STATUS_CHOICES = (
        (PENDING, 'Pending'),
        (ACCEPTED, 'Accepted'),
        (DECLINED, 'Declined'),
        (CANCELLED, 'Cancelled'),
    )

    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, related_name='teacher_requests')
    teacher = models.ForeignKey(
        Teacher, on_delete=models.CASCADE, related_name='learner_requests')
    school_class = models.ForeignKey(
        SchoolClass,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='learner_requests',
    )
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=PENDING)
    requested_at = models.DateTimeField(default=timezone.now)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-requested_at', '-pk']

    def __str__(self):
        return (
            f'{self.student} — {self.teacher} ({self.get_status_display()})')


class Subject(models.Model):
    name = models.CharField(max_length=120)
    teacher = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='subjects',
    )
    created_by = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_subjects',
    )
    school_class = models.ForeignKey(
        SchoolClass, on_delete=models.SET_NULL, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['teacher', 'school_class', 'name'],
                condition=Q(
                    teacher__isnull=False,
                    school_class__isnull=False,
                ),
                name='unique_configured_teacher_class_subject',
            ),
        ]

    def __str__(self):
        return self.name


class LoginActivity(models.Model):
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, related_name='login_activities')
    logged_in_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        ordering = ['-logged_in_at']
        verbose_name = 'learner login activity'
        verbose_name_plural = 'learner login activity'

    def __str__(self):
        return f'{self.student} - {self.logged_in_at}'


class ActivityLog(models.Model):
    ACTIONS = (
        ('create', 'Create'),
        ('update', 'Update'),
        ('delete', 'Delete'),
        ('sign_in', 'Sign in'),
        ('sign_out', 'Sign out'),
        ('failed_sign_in', 'Failed sign in'),
        ('export', 'Export'),
        ('import', 'Import'),
    )

    actor = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='activity_actions',
    )
    actor_username = models.CharField(max_length=150, blank=True, default='')
    target_user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='activity_events',
    )
    target_username = models.CharField(max_length=150, blank=True, default='')
    target_user_id_snapshot = models.CharField(
        max_length=64, blank=True, default='')
    action = models.CharField(max_length=20, choices=ACTIONS)
    object_type = models.CharField(max_length=100)
    object_id = models.CharField(max_length=64, blank=True, default='')
    description = models.CharField(max_length=255)
    changes = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        indexes = [
            models.Index(fields=['object_type', 'object_id']),
            models.Index(fields=['action', 'created_at']),
            models.Index(fields=['target_user', 'created_at']),
        ]

    def __str__(self):
        return f'{self.get_action_display()}: {self.description}'


class LeaveReportStudent(models.Model):
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, verbose_name='learner')
    date = models.CharField(max_length=60)
    message = models.TextField()
    status = models.SmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'learner leave request'
        verbose_name_plural = 'learner leave requests'


class FeedbackStudent(models.Model):
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, verbose_name='learner')
    feedback = models.TextField()
    reply = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'learner feedback'
        verbose_name_plural = 'learner feedback'


class NotificationStudent(models.Model):
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, verbose_name='learner')
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'learner notification'
        verbose_name_plural = 'learner notifications'


class StudentResult(models.Model):
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, verbose_name='learner')
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE)
    created_by = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_results',
    )
    academic_period = models.ForeignKey(
        AcademicPeriod,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='results',
    )
    test_1 = models.FloatField(default=0)
    test_2 = models.FloatField(default=0)
    test_3 = models.FloatField(default=0)
    test_4 = models.FloatField(default=0)
    test_5 = models.FloatField(default=0)
    test_scores = models.JSONField(default=dict, blank=True)
    assessment_scores = models.JSONField(default=dict, blank=True)
    is_previous_record = models.BooleanField(default=False)
    exam = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'learner result'
        verbose_name_plural = 'learner results'

    @property
    def test(self):
        return self.test_1

    @test.setter
    def test(self, value):
        self.test_1 = value


class AssessmentPlan(models.Model):
    school_class = models.ForeignKey(
        SchoolClass,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='assessment_plans',
    )
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name='assessment_plans',
    )
    academic_period = models.ForeignKey(
        AcademicPeriod,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='assessment_plans',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['school_class', 'subject', 'academic_period'],
                name='unique_class_subject_period_assessment_plan',
            ),
        ]
        ordering = [
            'school_class__grade_number',
            'school_class__class_letter',
            'academic_period__year',
            'academic_period__term',
            'subject__name',
        ]

    def __str__(self):
        return f'{self.school_class} - {self.subject} - {self.academic_period}'


class Assessment(models.Model):
    plan = models.ForeignKey(
        AssessmentPlan,
        on_delete=models.CASCADE,
        related_name='assessments',
    )
    name = models.CharField(max_length=100)
    weight = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    maximum_mark = models.DecimalField(
        max_digits=7, decimal_places=2, default=100)
    position = models.PositiveIntegerField(default=0)
    is_previous_record = models.BooleanField(default=False)

    class Meta:
        ordering = ['position', 'pk']
        constraints = [
            models.UniqueConstraint(
                fields=['plan', 'name'],
                name='unique_assessment_name_per_plan',
            ),
        ]

    def __str__(self):
        return self.name


class RecycleBinEntry(models.Model):
    owner = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='recycle_bin_entries',
    )
    record_type = models.CharField(max_length=40)
    record_label = models.CharField(max_length=255)
    object_count = models.PositiveIntegerField(default=0)
    snapshot = models.JSONField()
    deleted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-deleted_at', '-pk']

    def clean(self):
        from django.core.exceptions import ValidationError

        errors = {}
        if self.weight is not None and not Decimal('0') <= self.weight <= Decimal('100'):
            errors['weight'] = 'Weight must be between 0 and 100%.'
        if self.maximum_mark is not None and self.maximum_mark <= 0:
            errors['maximum_mark'] = 'Maximum mark must be greater than zero.'
        if errors:
            raise ValidationError(errors)


class SchoolProfile(models.Model):
    school_name = models.CharField(
        max_length=200,
        default='C. Heuva Junior Secondary School',
    )
    postal_address = models.TextField(blank=True)
    phone_numbers = models.CharField(max_length=200, blank=True)
    motto = models.CharField(max_length=200, blank=True)
    principal_name = models.CharField(max_length=150, blank=True)
    logo = models.ImageField(blank=True, upload_to='school_profile/')

    class Meta:
        verbose_name = 'school profile'
        verbose_name_plural = 'school profile'

    def __str__(self):
        return self.school_name


@receiver(post_save, sender=CustomUser)
def create_user_profile(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    if created:
        if instance.user_type == '1':
            Admin.objects.get_or_create(admin=instance)
        elif instance.user_type == '2':
            Student.objects.get_or_create(admin=instance)
        elif instance.user_type == '3':
            Teacher.objects.get_or_create(teacher=instance)


@receiver(post_save, sender=CustomUser)
def save_user_profile(sender, instance, raw=False, **kwargs):
    if raw:
        return
    if instance.user_type == '1':
        Admin.objects.get_or_create(admin=instance)
    elif instance.user_type == '2':
        Student.objects.get_or_create(admin=instance)
    elif instance.user_type == '3':
        Teacher.objects.get_or_create(teacher=instance)


@receiver(user_logged_in)
def record_learner_login(sender, request, user, **kwargs):
    if user.user_type != '2':
        return

    student, _ = Student.objects.get_or_create(admin=user)
    LoginActivity.objects.create(
        student=student,
        ip_address=request.META.get('REMOTE_ADDR') if request else None,
    )
