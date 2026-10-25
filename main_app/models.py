from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import UserManager
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from django.db.models.signals import post_save
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.validators import RegexValidator
from django.db.models import (
    Case,
    IntegerField,
    Q,
    Value,
    When,
)
from django.db.models.functions import Lower
from django.db.models.functions import Cast, Substr




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
        max_length=20,
        unique=True,
        blank=True,
        null=True,
        validators=[
            RegexValidator(
                regex=r'^[A-Za-z0-9][A-Za-z0-9._-]{3,19}$',
                message=(
                    'Usernames must be 4 to 20 characters and use only '
                    'letters, digits, dots, underscores, and hyphens.'
                ),
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


class Course(models.Model):
    name = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class Session(models.Model):
    start_year = models.DateField()
    end_year = models.DateField()

    def __str__(self):
        return "From " + str(self.start_year) + " to " + str(self.end_year)


def grade_ordered_courses():
    grade_number = Cast(Substr('name', 7), IntegerField())
    return Course.objects.annotate(
        _grade_order=Case(
            When(name__regex=r'^Grade [0-9]+$', then=grade_number),
            default=Value(1000),
            output_field=IntegerField(),
        ),
    ).order_by('_grade_order', 'name')



class Student(models.Model):
    admin = models.OneToOneField(CustomUser, on_delete=models.CASCADE)
    teacher = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='learners',
    )
    created_by = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_learners',
    )
    course = models.ForeignKey(
        Course, on_delete=models.SET_NULL, null=True, blank=True)
    session = models.ForeignKey(
        Session, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        verbose_name = 'learner'
        verbose_name_plural = 'learners'

    def __str__(self):
        return self.admin.last_name + ", " + self.admin.first_name


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
    course = models.ForeignKey(
        Course, on_delete=models.SET_NULL, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

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


class GradeBand(models.Model):
    symbol = models.CharField(max_length=10)
    minimum_score = models.FloatField()
    maximum_score = models.FloatField()
    description = models.CharField(max_length=120)
    remark = models.TextField(blank=True)

    class Meta:
        ordering = ['minimum_score', 'maximum_score', 'symbol']

    def __str__(self):
        return f'{self.symbol} ({self.minimum_score:g}-{self.maximum_score:g})'

    def clean(self):
        from django.core.exceptions import ValidationError

        errors = {}
        if (
            self.minimum_score is not None
            and self.maximum_score is not None
            and self.minimum_score > self.maximum_score
        ):
            errors['minimum_score'] = (
                'Minimum score cannot be greater than maximum score.')
        elif (
            self.minimum_score is not None
            and self.maximum_score is not None
            and GradeBand.objects.exclude(pk=self.pk).filter(
                minimum_score__lte=self.maximum_score,
                maximum_score__gte=self.minimum_score,
            ).exists()
        ):
            errors['minimum_score'] = (
                'This score range overlaps an existing grade band.')
        if errors:
            raise ValidationError(errors)


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
    test = models.FloatField(default=0)
    exam = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'learner result'
        verbose_name_plural = 'learner results'


class Attendance(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE)
    session = models.ForeignKey(Session, on_delete=models.CASCADE)
    created_by = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_attendance',
    )
    date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['course', 'subject', 'session', 'date'],
                name='unique_course_subject_session_attendance_date',
            ),
        ]
        ordering = ['-date', 'course__name', 'subject__name']

    def __str__(self):
        return f'{self.course} - {self.subject} - {self.date}'


class AttendanceReport(models.Model):
    attendance = models.ForeignKey(
        Attendance, on_delete=models.CASCADE, related_name='records')
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, related_name='attendance_records')
    created_by = models.ForeignKey(
        Teacher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_attendance_reports',
    )
    status = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['attendance', 'student'],
                name='unique_student_attendance_record',
            ),
        ]


@receiver(post_save, sender=CustomUser)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        if instance.user_type == '1':
            Admin.objects.get_or_create(admin=instance)
        elif instance.user_type == '2':
            Student.objects.get_or_create(admin=instance)
        elif instance.user_type == '3':
            Teacher.objects.get_or_create(teacher=instance)


@receiver(post_save, sender=CustomUser)
def save_user_profile(sender, instance, **kwargs):
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
