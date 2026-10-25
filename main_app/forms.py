import re

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.forms.widgets import DateInput, TextInput

from .credentials import verify_pin
from .models import *
from .models import grade_ordered_courses


def validate_unique_username(value, user=None):
    username = value.strip().lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{3,19}', username):
        raise forms.ValidationError(
            'Use 4-20 letters, digits, dots, underscores, or hyphens; '
            'the first character must be a letter or digit.')
    matching_users = CustomUser.objects.filter(username__iexact=username)
    if user:
        matching_users = matching_users.exclude(pk=user.pk)
    if matching_users.exists():
        raise forms.ValidationError(
            'An account with this username already exists.')
    return username


class FormSettings(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super(FormSettings, self).__init__(*args, **kwargs)
        # Here make some changes such as:
        for field in self.visible_fields():
            field.field.widget.attrs['class'] = 'form-control'


class CustomUserForm(FormSettings):
    username = forms.CharField(
        min_length=4,
        max_length=20,
        label='Username',
        help_text=(
            '4-20 characters; letters, digits, dots, underscores, and '
            'hyphens only; start with a letter or digit.'
        ),
    )
    gender = forms.ChoiceField(choices=[('M', 'Male'), ('F', 'Female')])
    first_name = forms.CharField(required=True)
    last_name = forms.CharField(required=True)
    address = forms.CharField(required=False, widget=forms.Textarea)
    password = forms.CharField(widget=forms.PasswordInput)
    widget = {
        'password': forms.PasswordInput(),
    }
    profile_pic = forms.ImageField(required=False)

    def __init__(self, *args, **kwargs):
        super(CustomUserForm, self).__init__(*args, **kwargs)

        if kwargs.get('instance'):
            self.account_user = kwargs.get('instance').admin
            instance = self.account_user.__dict__
            self.fields['password'].required = False
            for field in CustomUserForm.Meta.fields:
                self.fields[field].initial = instance.get(field)
            if self.instance.pk is not None:
                self.fields['password'].widget.attrs['placeholder'] = "Fill this only if you wish to update password"
        else:
            self.account_user = None

    def clean_username(self):
        return validate_unique_username(
            self.cleaned_data['username'],
            user=self.account_user,
        )

    class Meta:
        model = CustomUser
        fields = [
            'first_name', 'last_name', 'username', 'gender', 'password',
            'profile_pic', 'address',
        ]


class StudentForm(CustomUserForm):
    def __init__(self, *args, **kwargs):
        super(StudentForm, self).__init__(*args, **kwargs)
        self.fields['course'].queryset = grade_ordered_courses()

    class Meta(CustomUserForm.Meta):
        model = Student
        fields = CustomUserForm.Meta.fields + ['course', 'session']


class AdminForm(CustomUserForm):
    def __init__(self, *args, **kwargs):
        super(AdminForm, self).__init__(*args, **kwargs)

    class Meta(CustomUserForm.Meta):
        model = Admin
        fields = CustomUserForm.Meta.fields


class SubjectForm(FormSettings):
    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['course'].queryset = grade_ordered_courses()

    class Meta:
        model = Subject
        fields = ['name', 'course']


class StudentResultForm(FormSettings):
    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user and user.user_type == '3':
            self.fields['student'].queryset = Student.objects.filter(
                teacher__teacher=user).select_related('admin')
            self.fields['subject'].queryset = Subject.objects.filter(
                Q(teacher__teacher=user) | Q(teacher__isnull=True))

    def clean(self):
        cleaned_data = super().clean()
        student = cleaned_data.get('student')
        subject = cleaned_data.get('subject')
        if (
            student and subject and subject.course_id
            and subject.course_id != student.course_id
        ):
            self.add_error(
                'subject',
                "Choose a subject assigned to the learner's grade or school-wide.",
            )
        return cleaned_data

    class Meta:
        model = StudentResult
        fields = ['student', 'subject', 'test', 'exam']
        labels = {
            'student': 'Learner',
            'test': 'Test / CA score',
            'exam': 'Exam score',
        }


class ReportGenerationForm(forms.Form):
    REPORT_TYPES = (
        ('school_summary', 'School Summary'),
        ('class_list', 'Class List'),
        ('learner_results', 'Learner Results'),
        ('attendance_summary', 'Attendance Summary'),
    )

    report_type = forms.ChoiceField(choices=REPORT_TYPES, label='Report type')
    course = forms.ModelChoiceField(
        queryset=Course.objects.all(), required=False, label='Course / grade')
    session = forms.ModelChoiceField(
        queryset=Session.objects.all(), required=False, label='Session / term')
    subject = forms.ModelChoiceField(
        queryset=Subject.objects.all(), required=False, label='Subject')

    def __init__(self, *args, role='admin', user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['course'].queryset = grade_ordered_courses()
        if role == 'teacher':
            self.fields['report_type'].choices = (
                ('class_list', 'Class List'),
                ('learner_results', 'Learner Results'),
                ('attendance_summary', 'Attendance Summary'),
            )
            if user:
                self.fields['course'].queryset = self.fields[
                    'course'].queryset.filter(
                        student__teacher__teacher=user).distinct()
                self.fields['subject'].queryset = Subject.objects.filter(
                    Q(teacher__teacher=user) | Q(teacher__isnull=True))
        else:
            self.fields['report_type'].choices = (
                ('school_summary', 'School Summary'),
                ('class_list', 'Class List'),
            )
        self.fields['report_type'].widget.attrs['class'] = 'form-control'
        for name in ('course', 'session', 'subject'):
            self.fields[name].widget.attrs['class'] = 'form-control'
        if self.is_bound:
            course_id = self.data.get('course')
            if course_id:
                self.fields['subject'].queryset = Subject.objects.filter(
                    Q(course_id=course_id) | Q(course__isnull=True))
                if role == 'teacher' and user:
                    self.fields['subject'].queryset = self.fields[
                        'subject'].queryset.filter(
                            Q(teacher__teacher=user)
                            | Q(teacher__isnull=True))

    def clean(self):
        cleaned_data = super().clean()
        report_type = cleaned_data.get('report_type')
        if report_type == 'school_summary':
            return cleaned_data

        if report_type != 'school_summary' and not cleaned_data.get('course'):
            self.add_error('course', 'Choose a course or grade.')
        if report_type != 'school_summary' and not cleaned_data.get('session'):
            self.add_error('session', 'Choose a session or term.')
        return cleaned_data


class LoginActivityFilterForm(forms.Form):
    learner = forms.CharField(
        required=False,
        max_length=120,
        label='Learner name or username',
    )
    date = forms.DateField(
        required=False,
        widget=DateInput(attrs={'type': 'date'}),
        label='Login date',
    )
    course = forms.ModelChoiceField(
        queryset=grade_ordered_courses(),
        required=False,
        label='Course / grade',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['course'].queryset = grade_ordered_courses()
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'


class ActivityLogFilterForm(forms.Form):
    search = forms.CharField(required=False, max_length=120, label='Search')
    action = forms.ChoiceField(
        required=False,
        choices=(('', 'All activity'),) + ActivityLog.ACTIONS,
    )
    start_date = forms.DateField(
        required=False,
        widget=DateInput(attrs={'type': 'date'}),
        label='From',
    )
    end_date = forms.DateField(
        required=False,
        widget=DateInput(attrs={'type': 'date'}),
        label='To',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get('start_date')
        end_date = cleaned_data.get('end_date')
        if start_date and end_date and start_date > end_date:
            self.add_error('end_date', 'The end date must be on or after the start date.')
        return cleaned_data


class GradeBandForm(forms.ModelForm):
    class Meta:
        model = GradeBand
        fields = [
            'symbol', 'minimum_score', 'maximum_score', 'description', 'remark',
        ]
        widgets = {
            'remark': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'


class ReportCardForm(forms.Form):
    student = forms.ModelChoiceField(
        queryset=Student.objects.select_related('admin').order_by(
            'admin__last_name', 'admin__first_name'),
        label='Learner',
    )
    session = forms.ModelChoiceField(
        queryset=Session.objects.order_by('-start_year'),
        label='Session / term',
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user and user.user_type == '3':
            self.fields['student'].queryset = Student.objects.filter(
                teacher__teacher=user).select_related('admin').order_by(
                    'admin__last_name', 'admin__first_name')
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'


class AttendanceSelectionForm(forms.Form):
    course = forms.ModelChoiceField(
        queryset=grade_ordered_courses(),
        label='Course / grade',
    )
    subject = forms.ModelChoiceField(
        queryset=Subject.objects.order_by('name'),
        label='Subject',
    )
    session = forms.ModelChoiceField(
        queryset=Session.objects.order_by('-start_year'),
        label='Session / term',
    )
    date = forms.DateField(
        widget=DateInput(attrs={'type': 'date'}),
        label='Attendance date',
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user and user.user_type == '3':
            self.fields['course'].queryset = grade_ordered_courses().filter(
                student__teacher__teacher=user).distinct()
            self.fields['subject'].queryset = Subject.objects.filter(
                Q(teacher__teacher=user) | Q(teacher__isnull=True))
        course_id = self.data.get('course') if self.is_bound else None
        if course_id:
            self.fields['subject'].queryset = Subject.objects.filter(
                Q(course_id=course_id) | Q(course__isnull=True)
            ).order_by('name')
            if user and user.user_type == '3':
                self.fields['subject'].queryset = self.fields[
                    'subject'].queryset.filter(
                        Q(teacher__teacher=user) | Q(teacher__isnull=True))
            if user and user.user_type == '3':
                self.fields['subject'].queryset = self.fields[
                    'subject'].queryset.filter(
                        Q(teacher__teacher=user) | Q(teacher__isnull=True))
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'

    def clean(self):
        cleaned_data = super().clean()
        course = cleaned_data.get('course')
        subject = cleaned_data.get('subject')
        if course and subject and subject.course_id not in (None, course.pk):
            self.add_error('subject', 'Choose a subject for this course/grade.')
        return cleaned_data


class TeacherGradeFilterForm(forms.Form):
    teacher = forms.ModelChoiceField(
        queryset=Teacher.objects.select_related('teacher').order_by(
            'teacher__last_name', 'teacher__first_name'),
        required=False,
        empty_label='All teachers',
    )
    course = forms.ModelChoiceField(
        queryset=grade_ordered_courses(),
        required=False,
        label='Grade',
        empty_label='All grades',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'


class LearnerAssignmentForm(forms.Form):
    learners = forms.ModelMultipleChoiceField(
        queryset=Student.objects.select_related('admin').order_by(
            'admin__last_name', 'admin__first_name'),
        widget=forms.SelectMultiple(attrs={'size': 12}),
    )
    teacher = forms.ModelChoiceField(
        queryset=Teacher.objects.select_related('teacher').order_by(
            'teacher__last_name', 'teacher__first_name'),
        required=False,
        empty_label='Unassigned',
    )


class SubjectAssignmentForm(forms.Form):
    subjects = forms.ModelMultipleChoiceField(
        queryset=Subject.objects.order_by('name'),
        widget=forms.SelectMultiple(attrs={'size': 12}),
    )
    teacher = forms.ModelChoiceField(
        queryset=Teacher.objects.select_related('teacher').order_by(
            'teacher__last_name', 'teacher__first_name'),
        required=False,
        empty_label='School-wide / unassigned',
    )


class LeaveReportStudentForm(FormSettings):
    def __init__(self, *args, **kwargs):
        super(LeaveReportStudentForm, self).__init__(*args, **kwargs)

    class Meta:
        model = LeaveReportStudent
        fields = ['date', 'message']
        widgets = {
            'date': DateInput(attrs={'type': 'date'}),
        }


class FeedbackStudentForm(FormSettings):

    def __init__(self, *args, **kwargs):
        super(FeedbackStudentForm, self).__init__(*args, **kwargs)

    class Meta:
        model = FeedbackStudent
        fields = ['feedback']


class StudentEditForm(CustomUserForm):
    def __init__(self, *args, **kwargs):
        super(StudentEditForm, self).__init__(*args, **kwargs)

    class Meta(CustomUserForm.Meta):
        model = Student
        fields = CustomUserForm.Meta.fields 


class RegistrationForm(forms.Form):
    first_name = forms.CharField(max_length=150, label='First name')
    last_name = forms.CharField(max_length=150, label='Last name')
    username = forms.CharField(
        min_length=4,
        max_length=20,
        label='Username',
        help_text=(
            '4-20 characters; letters, digits, dots, underscores, and '
            'hyphens only; start with a letter or digit.'
        ),
    )
    gender = forms.ChoiceField(
        choices=CustomUser.GENDER,
        widget=forms.RadioSelect,
    )
    password = forms.CharField(widget=forms.PasswordInput)
    confirm_password = forms.CharField(
        label='Confirm password',
        widget=forms.PasswordInput,
    )
    profile_pic = forms.ImageField(
        required=False,
        label='Profile picture',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name != 'gender':
                field.widget.attrs['class'] = 'form-control'
        self.fields['profile_pic'].help_text = (
            'Optional. JPG, JPEG, or PNG; maximum file size 5 MB.')

    def clean_username(self):
        return validate_unique_username(self.cleaned_data['username'])

    def clean_profile_pic(self):
        picture = self.cleaned_data.get('profile_pic')
        if not picture:
            return picture
        if picture.size > 5 * 1024 * 1024:
            raise forms.ValidationError(
                'Profile picture must be no larger than 5 MB.')
        extension = picture.name.rsplit('.', 1)[-1].lower()
        if extension not in ('jpg', 'jpeg', 'png'):
            raise forms.ValidationError(
                'Choose a JPG, JPEG, or PNG image.')
        if getattr(picture.image, 'format', '').upper() not in ('JPEG', 'PNG'):
            raise forms.ValidationError(
                'The uploaded file must be a valid JPG, JPEG, or PNG image.')
        return picture

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get('password')
        confirm_password = cleaned_data.get('confirm_password')
        if password and confirm_password and password != confirm_password:
            self.add_error('confirm_password', 'Passwords do not match.')
        if password:
            user = CustomUser(
                username=cleaned_data.get('username', ''),
                first_name=cleaned_data.get('first_name', ''),
                last_name=cleaned_data.get('last_name', ''),
            )
            try:
                validate_password(password, user=user)
            except ValidationError as error:
                self.add_error('password', error)
        return cleaned_data


class LearnerRegistrationForm(RegistrationForm):
    course = forms.ModelChoiceField(
        queryset=grade_ordered_courses(),
        label='Grade',
        empty_label='Select a grade',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['course'].queryset = grade_ordered_courses()
        self.has_grades = self.fields['course'].queryset.exists()
        self.order_fields([
            'first_name', 'last_name', 'username', 'gender',
            'password', 'confirm_password', 'course', 'profile_pic',
        ])


class TeacherAccountForm(forms.Form):
    first_name = forms.CharField(max_length=150, label='First name')
    last_name = forms.CharField(max_length=150, label='Last name')
    username = forms.CharField(
        min_length=4,
        max_length=20,
        label='Username',
        help_text=(
            '4-20 characters; letters, digits, dots, underscores, and '
            'hyphens only; start with a letter or digit.'
        ),
    )
    gender = forms.ChoiceField(
        choices=CustomUser.GENDER,
        widget=forms.RadioSelect,
    )
    profile_pic = forms.ImageField(required=False, label='Profile picture')

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if user:
            self.fields['first_name'].initial = user.first_name
            self.fields['last_name'].initial = user.last_name
            self.fields['username'].initial = user.username
            self.fields['gender'].initial = user.gender
        self.fields['profile_pic'].help_text = (
            'Optional. JPG, JPEG, or PNG; maximum file size 5 MB.')
        for name, field in self.fields.items():
            if name != 'gender':
                field.widget.attrs['class'] = 'form-control'

    def clean_username(self):
        return validate_unique_username(
            self.cleaned_data['username'],
            user=self.user,
        )

    def clean_profile_pic(self):
        picture = self.cleaned_data.get('profile_pic')
        if not picture:
            return picture
        if picture.size > 5 * 1024 * 1024:
            raise forms.ValidationError(
                'Profile picture must be no larger than 5 MB.')
        extension = picture.name.rsplit('.', 1)[-1].lower()
        if extension not in ('jpg', 'jpeg', 'png'):
            raise forms.ValidationError('Choose a JPG, JPEG, or PNG image.')
        if getattr(picture.image, 'format', '').upper() not in ('JPEG', 'PNG'):
            raise forms.ValidationError(
                'The uploaded file must be a valid JPG, JPEG, or PNG image.')
        return picture

class ProfileUpdateForm(forms.Form):
    first_name = forms.CharField(max_length=150, label='First name')
    last_name = forms.CharField(max_length=150, label='Last name')
    profile_pic = forms.ImageField(
        required=False,
        label='Profile picture',
    )
    current_password = forms.CharField(
        required=False,
        label='Current password',
        widget=forms.PasswordInput,
    )
    new_password = forms.CharField(
        required=False,
        label='New password',
        widget=forms.PasswordInput,
    )
    confirm_password = forms.CharField(
        required=False,
        label='Confirm new password',
        widget=forms.PasswordInput,
    )
    current_pin = forms.CharField(
        required=False,
        label='Current PIN',
        widget=forms.PasswordInput,
    )
    reset_pin = forms.BooleanField(
        required=False,
        label='Generate a new PIN',
    )

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.fields['first_name'].initial = user.first_name
        self.fields['last_name'].initial = user.last_name
        self.fields['profile_pic'].help_text = (
            'Optional. JPG, JPEG, or PNG; maximum file size 5 MB.')
        if user.user_type == '1':
            for name in (
                'current_password',
                'new_password',
                'confirm_password',
                'current_pin',
                'reset_pin',
            ):
                self.fields.pop(name)
        elif user.user_type == '3':
            for name in (
                'current_password',
                'new_password',
                'confirm_password',
            ):
                self.fields.pop(name)
        else:
            self.fields.pop('current_pin')
            self.fields.pop('reset_pin')
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'

    def clean_profile_pic(self):
        picture = self.cleaned_data.get('profile_pic')
        if not picture:
            return picture
        if picture.size > 5 * 1024 * 1024:
            raise forms.ValidationError(
                'Profile picture must be no larger than 5 MB.')
        extension = picture.name.rsplit('.', 1)[-1].lower()
        if extension not in ('jpg', 'jpeg', 'png'):
            raise forms.ValidationError(
                'Choose a JPG, JPEG, or PNG image.')
        if getattr(picture.image, 'format', '').upper() not in ('JPEG', 'PNG'):
            raise forms.ValidationError(
                'The uploaded file must be a valid JPG, JPEG, or PNG image.')
        return picture

    def clean(self):
        cleaned_data = super().clean()
        if self.user.user_type == '1':
            return cleaned_data
        if self.user.user_type == '3':
            if not cleaned_data.get('reset_pin'):
                return cleaned_data
            current_pin = cleaned_data.get('current_pin')
            if not self.user.pin_hash:
                self.add_error(
                    'current_pin',
                    'No current PIN is set. Ask an administrator to issue one.')
            elif not current_pin:
                self.add_error('current_pin', 'Enter your current PIN.')
            elif not verify_pin(current_pin, self.user.pin_hash):
                self.add_error('current_pin', 'Current PIN is incorrect.')
            return cleaned_data

        current_password = cleaned_data.get('current_password')
        new_password = cleaned_data.get('new_password')
        confirm_password = cleaned_data.get('confirm_password')
        password_change_requested = bool(new_password or confirm_password)

        if not password_change_requested:
            return cleaned_data
        if not current_password:
            self.add_error('current_password', 'Enter your current password.')
        elif not self.user.check_password(current_password):
            self.add_error('current_password', 'Current password is incorrect.')

        if password_change_requested:
            if not new_password:
                self.add_error('new_password', 'Enter a new password.')
            if new_password and not confirm_password:
                self.add_error('confirm_password', 'Confirm your new password.')
            elif new_password and confirm_password and new_password != confirm_password:
                self.add_error('confirm_password', 'Passwords do not match.')
        if new_password:
            try:
                validate_password(new_password, user=self.user)
            except ValidationError as error:
                self.add_error('new_password', error)

        return cleaned_data


class StaffCredentialResetForm(forms.Form):
    ACTIONS = (
        ('password', 'Set new password'),
        ('pin', 'Generate new PIN'),
    )
    action = forms.ChoiceField(choices=ACTIONS, label='Reset')
    new_password = forms.CharField(
        required=False,
        label='New password',
        widget=forms.PasswordInput,
    )
    confirm_password = forms.CharField(
        required=False,
        label='Confirm new password',
        widget=forms.PasswordInput,
    )

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if user.user_type == '2':
            self.fields['action'].choices = (('password', 'Set new password'),)
        else:
            self.fields['action'].choices = (('pin', 'Generate new PIN'),)
            self.fields.pop('new_password')
            self.fields.pop('confirm_password')
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get('action') != 'password':
            return cleaned_data

        password = cleaned_data.get('new_password')
        confirmation = cleaned_data.get('confirm_password')
        if not password:
            self.add_error('new_password', 'Enter a new password.')
        if password and not confirmation:
            self.add_error('confirm_password', 'Confirm the new password.')
        elif password and confirmation and password != confirmation:
            self.add_error('confirm_password', 'Passwords do not match.')
        if password:
            try:
                validate_password(password, user=self.user)
            except ValidationError as error:
                self.add_error('new_password', error)
        return cleaned_data
