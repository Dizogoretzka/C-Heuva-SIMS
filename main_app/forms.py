from django import forms
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.forms.models import ModelChoiceIterator
from django.forms.widgets import DateInput, TextInput

from .credentials import verify_pin
from .learner_ordering import sort_learners
from .models import *
from .class_parser import (
    ClassLabelError,
    format_class_label,
    parse_class_label,
)
from .models import ordered_school_classes
from .security_questions import (
    SECURITY_QUESTIONS,
    normalize_security_answer,
)


class FormSettings(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super(FormSettings, self).__init__(*args, **kwargs)
        # Here make some changes such as:
        for field in self.visible_fields():
            field.field.widget.attrs['class'] = 'form-control'


class CustomUserForm(FormSettings):
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

    class Meta:
        model = CustomUser
        fields = [
            'first_name', 'last_name', 'gender', 'password',
            'profile_pic', 'address',
        ]


class StudentForm(CustomUserForm):
    grade_and_class = forms.CharField(
        required=False,
        label='Grade and class',
        help_text='Examples: 8A, 8 a, 9',
    )

    def __init__(self, *args, **kwargs):
        super(StudentForm, self).__init__(*args, **kwargs)
        school_class = getattr(self.instance, 'school_class', None)
        if school_class and school_class.grade_number is not None:
            self.fields['grade_and_class'].initial = (
                f'{school_class.grade_number}{school_class.class_letter.upper()}'
            )
        if not self.is_bound and not self.instance.academic_period_id:
            self.fields['academic_period'].initial = AcademicPeriod.objects.filter(
                is_current=True).first()

    def clean_grade_and_class(self):
        value = self.cleaned_data.get('grade_and_class', '')
        try:
            parse_class_label(value)
        except ClassLabelError as error:
            raise forms.ValidationError(str(error)) from error
        return value

    class Meta(CustomUserForm.Meta):
        model = Student
        fields = (
            CustomUserForm.Meta.fields
            + ['grade_and_class', 'academic_period']
        )


class AdminForm(CustomUserForm):
    def __init__(self, *args, **kwargs):
        super(AdminForm, self).__init__(*args, **kwargs)

    class Meta(CustomUserForm.Meta):
        model = Admin
        fields = CustomUserForm.Meta.fields


class ManageResultsFilterForm(forms.Form):
    school_class = forms.ModelChoiceField(
        queryset=ordered_school_classes(),
        required=False,
        label='Class',
    )
    subject = forms.ModelChoiceField(
        queryset=Subject.objects.none(),
        required=False,
        label='Subject',
    )
    academic_period = forms.ModelChoiceField(
        queryset=AcademicPeriod.objects.order_by(
            '-year', '-term', '-start_year'),
        required=False,
        label='Academic period',
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        classes = ordered_school_classes()
        subjects = Subject.objects.filter(
            school_class__isnull=False).select_related(
                'school_class', 'teacher__teacher')
        if user and user.user_type == '3':
            classes = classes.filter(
                teacher_setups__teacher__teacher=user).distinct()
            subjects = subjects.filter(
                teacher__teacher=user,
                school_class__teacher_setups__teacher__teacher=user,
            ).distinct()
        self.fields['school_class'].queryset = classes
        self.fields['subject'].queryset = subjects
        if self.is_bound:
            class_id = self.data.get(self.add_prefix('school_class'), '')
            if str(class_id).isdigit():
                self.fields['subject'].queryset = subjects.filter(
                    school_class_id=int(class_id))
        else:
            current_period = AcademicPeriod.objects.filter(
                is_current=True).first()
            if current_period:
                self.fields['academic_period'].initial = current_period
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'

    def clean(self):
        cleaned_data = super().clean()
        school_class = cleaned_data.get('school_class')
        subject = cleaned_data.get('subject')
        if (
            school_class and subject
            and subject.school_class_id != school_class.pk
        ):
            self.add_error(
                'subject', 'Choose a subject set up for the selected class.')
        return cleaned_data


class ReportGenerationForm(forms.Form):
    REPORT_TYPES = (
        ('school_summary', 'School Summary'),
        ('class_list', 'Class List'),
        ('learner_results', 'Results Mark Sheet'),
    )

    report_type = forms.ChoiceField(choices=REPORT_TYPES, label='Report type')
    school_class = forms.ModelChoiceField(
        queryset=ordered_school_classes(),
        required=False,
        label='Class',
    )
    academic_period = forms.ModelChoiceField(
        queryset=AcademicPeriod.objects.all(), required=False, label='Academic Period')
    subject = forms.ModelChoiceField(
        queryset=Subject.objects.all(), required=False, label='Subject')

    def __init__(self, *args, role='admin', user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['school_class'].queryset = ordered_school_classes()
        if not self.is_bound:
            self.fields['academic_period'].initial = (
                AcademicPeriod.objects.filter(is_current=True).first())
        if role == 'teacher':
            self.fields['report_type'].choices = (
                ('school_summary', 'School Summary'),
                ('class_list', 'Class List'),
                ('learner_results', 'Results Mark Sheet'),
            )
            if user:
                self.fields['school_class'].queryset = self.fields[
                    'school_class'].queryset.filter(
                        teacher_setups__teacher__teacher=user).distinct()
                self.fields['subject'].queryset = Subject.objects.filter(
                    teacher__teacher=user,
                    school_class__in=self.fields['school_class'].queryset,
                    school_class__teacher_setups__teacher__teacher=user,
                )
        else:
            self.fields['report_type'].choices = (
                ('school_summary', 'School Summary'),
                ('class_list', 'Class List'),
                ('learner_results', 'Results Mark Sheet'),
            )
        self.fields['report_type'].widget.attrs['class'] = 'form-control'
        for name in ('school_class', 'academic_period', 'subject'):
            self.fields[name].widget.attrs['class'] = 'form-control'
        if self.is_bound:
            school_class_id = self.data.get('school_class')
            if school_class_id and str(school_class_id).isdigit():
                if role == 'teacher' and user:
                    self.fields['subject'].queryset = Subject.objects.filter(
                        school_class_id=school_class_id,
                        teacher__teacher=user,
                        school_class__teacher_setups__teacher__teacher=user,
                    )
                else:
                    self.fields['subject'].queryset = Subject.objects.filter(
                        school_class_id=school_class_id)

    def clean(self):
        cleaned_data = super().clean()
        report_type = cleaned_data.get('report_type')
        if report_type == 'school_summary':
            return cleaned_data

        if (
            report_type != 'school_summary'
            and not cleaned_data.get('school_class')
        ):
            self.add_error('school_class', 'Choose a class.')
        if report_type != 'school_summary' and not cleaned_data.get('academic_period'):
            self.add_error('academic_period', 'Choose an academic period.')
        if (
            report_type == 'learner_results'
            and not cleaned_data.get('subject')
        ):
            self.add_error('subject', 'Choose a subject for the results mark sheet.')
        return cleaned_data


class ActivityLogFilterForm(forms.Form):
    search = forms.CharField(required=False, max_length=120, label='Search')
    role = forms.ChoiceField(
        required=False,
        label='Role',
        choices=(
            ('', 'All roles'),
            ('2', 'Learner'),
            ('3', 'Teacher'),
            ('1', 'Administrator'),
        ),
    )
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


class LearnerModelChoiceIterator(ModelChoiceIterator):
    def __iter__(self):
        if self.field.empty_label is not None:
            yield ('', self.field.empty_label)
        learners = sort_learners(
            self.queryset.select_related('admin'),
            lambda learner: learner.admin,
        )
        for learner in learners:
            yield self.choice(learner)


class LearnerModelChoiceField(forms.ModelChoiceField):
    iterator = LearnerModelChoiceIterator


class ReportCardForm(forms.Form):
    student = LearnerModelChoiceField(
        queryset=Student.objects.select_related('admin').order_by(
            'admin__last_name', 'admin__first_name'),
        label='Learner',
    )
    academic_period = forms.ModelChoiceField(
        queryset=AcademicPeriod.objects.order_by('-start_year'),
        label='Academic Period',
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.fields['academic_period'].initial = (
                AcademicPeriod.objects.filter(is_current=True).first())
        if user and user.user_type == '3':
            self.fields['student'].queryset = Student.objects.filter(
                teacher_requests__teacher__teacher=user,
                teacher_requests__status=LearnerTeacherRequest.ACCEPTED,
            ).select_related('admin').distinct().order_by(
                    'admin__last_name', 'admin__first_name')
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'


class ReportCardPublicationForm(forms.Form):
    school_class = forms.ModelChoiceField(
        queryset=ordered_school_classes(),
        required=False,
        label='Class',
    )
    academic_period = forms.ModelChoiceField(
        queryset=AcademicPeriod.objects.order_by('-year', '-term'),
        label='Academic period',
    )
    school_wide = forms.BooleanField(
        required=False,
        label='All classes and teachers',
    )

    def __init__(self, *args, user=None, **kwargs):
        kwargs.setdefault('auto_id', 'publication_%s')
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields['school_class'].queryset = ordered_school_classes()
        if user and user.user_type == '3':
            self.fields['school_class'].required = True
            self.fields['school_class'].queryset = self.fields[
                'school_class'].queryset.filter(
                    teacher_setups__teacher__teacher=user).distinct()
            self.fields.pop('school_wide')
        else:
            self.fields['school_wide'].widget.attrs['class'] = (
                'form-check-input')
        if not self.is_bound:
            self.fields['academic_period'].initial = (
                AcademicPeriod.objects.filter(is_current=True).first())
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')

    def clean(self):
        cleaned_data = super().clean()
        if (
            self.user
            and self.user.user_type == '1'
            and not cleaned_data.get('school_wide')
            and not cleaned_data.get('school_class')
        ):
            self.add_error('school_class', 'Choose a class or all classes.')
        return cleaned_data


class AcademicPeriodForm(FormSettings):
    class Meta:
        model = AcademicPeriod
        fields = ['term', 'year']
        widgets = {
            'year': forms.NumberInput(attrs={'min': 1, 'step': 1}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['year'].required = not self.instance.pk
        self.fields['term'].required = not self.instance.pk

    def clean(self):
        cleaned_data = super().clean()
        year = cleaned_data.get('year')
        term = cleaned_data.get('term')
        if (year is None) != (term is None):
            message = 'Choose both the year and term.'
            if year is None:
                self.add_error('year', message)
            if term is None:
                self.add_error('term', message)
        elif year is None and not self.instance.pk:
            self.add_error('year', 'Choose a year and term.')
            self.add_error('term', 'Choose a year and term.')
        return cleaned_data


class SchoolProfileForm(FormSettings):
    class Meta:
        model = SchoolProfile
        fields = (
            'school_name', 'postal_address', 'phone_numbers', 'motto',
            'principal_name', 'logo',
        )
        widgets = {
            'postal_address': forms.Textarea(attrs={'rows': 3}),
        }


class LearnerImportUploadForm(forms.Form):
    file = forms.FileField(
        label='Learner CSV or XLSX file',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        maximum_bytes = settings.SIMS_LEARNER_IMPORT_MAX_BYTES
        maximum_megabytes = maximum_bytes / (1024 * 1024)
        self.fields['file'].help_text = (
            f'Maximum size: {maximum_megabytes:g} MB.')

    def clean_file(self):
        uploaded_file = self.cleaned_data['file']
        if uploaded_file.size > settings.SIMS_LEARNER_IMPORT_MAX_BYTES:
            maximum_megabytes = (
                settings.SIMS_LEARNER_IMPORT_MAX_BYTES / (1024 * 1024))
            raise ValidationError(
                f'The upload exceeds the {maximum_megabytes:g} MB file-size '
                'limit.')
        if uploaded_file.name.rsplit('.', 1)[-1].lower() not in ('csv', 'xlsx'):
            raise ValidationError('Choose a .csv or .xlsx file.')
        return uploaded_file


class LearnerImportOptionsForm(forms.Form):
    academic_period = forms.ModelChoiceField(
        queryset=AcademicPeriod.objects.order_by('-year', 'term'),
        label='Academic Period',
    )
    class_for_blank_rows = forms.CharField(
        required=False,
        label='Class to use for rows without a class',
        max_length=32,
    )
    confirm_new_classes = forms.BooleanField(
        required=False,
        label='Add the selected class to My Classes and Subjects',
    )

    def __init__(
        self,
        *args,
        user=None,
        missing_class_count=0,
        imported_class_labels=(),
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.user = user
        self.missing_class_count = missing_class_count
        self.imported_class_labels = {
            label for label in imported_class_labels if label
        }
        setups = TeacherClassSetup.objects.select_related(
            'school_class').order_by(
                'school_class__grade_number', 'school_class__class_letter')
        if user and user.user_type == '3':
            setups = setups.filter(teacher__teacher=user)
        else:
            setups = setups.filter(
                school_class__teacher_setups__isnull=False).distinct()
        self.class_suggestions = list(dict.fromkeys(
            str(setup.school_class) for setup in setups))
        self.existing_class_labels = set(self.class_suggestions)
        self.new_class_labels = sorted(
            self.imported_class_labels - self.existing_class_labels)
        self.new_class_confirmation_required = bool(
            user and user.user_type == '3' and self.new_class_labels)
        if missing_class_count:
            self.fields['class_for_blank_rows'].help_text = (
                f'Applied to {missing_class_count} rows with no class.')
            self.fields['class_for_blank_rows'].widget.attrs[
                'list'] = 'learner-import-class-options'
        else:
            self.fields.pop('class_for_blank_rows')
        if not user or user.user_type != '3':
            self.fields.pop('confirm_new_classes')
        if not self.is_bound:
            self.fields['academic_period'].initial = (
                AcademicPeriod.objects.filter(is_current=True).first())
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'

    def clean(self):
        cleaned_data = super().clean()
        class_labels = set(self.imported_class_labels)
        selected_class = cleaned_data.get('class_for_blank_rows')
        if selected_class:
            class_labels.add(selected_class)
        self.new_class_labels = sorted(
            class_labels - self.existing_class_labels)
        self.new_class_confirmation_required = bool(
            self.user
            and self.user.user_type == '3'
            and self.new_class_labels
        )
        if (
            self.new_class_confirmation_required
            and not cleaned_data.get('confirm_new_classes')
        ):
            self.add_error(
                'confirm_new_classes',
                'Confirm that the new class will be added to My Classes and Subjects.',
            )
        return cleaned_data

    def clean_class_for_blank_rows(self):
        value = self.cleaned_data.get('class_for_blank_rows', '')
        if not self.missing_class_count:
            return ''
        if not value:
            raise ValidationError('Choose a class for the rows without one.')
        try:
            parsed = parse_class_label(value)
        except ClassLabelError as error:
            raise ValidationError(str(error)) from error
        if parsed is None:
            raise ValidationError('Choose a class for the rows without one.')
        return format_class_label(*parsed)


class TeacherGradeFilterForm(forms.Form):
    teacher = forms.ModelChoiceField(
        queryset=Teacher.objects.select_related('teacher').order_by(
            'teacher__last_name', 'teacher__first_name'),
        required=False,
        empty_label='All teachers',
    )
    school_class = forms.ModelChoiceField(
        queryset=ordered_school_classes(),
        required=False,
        label='Class',
        empty_label='All classes',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'


class TeacherClassSetupForm(forms.Form):
    grade_and_class = forms.CharField(
        label='Grade and class',
        help_text='Examples: 8A, 8 a, 9',
        max_length=32,
    )

    def clean_grade_and_class(self):
        value = self.cleaned_data['grade_and_class']
        try:
            parsed = parse_class_label(value)
        except ClassLabelError as error:
            raise ValidationError(str(error)) from error
        if parsed is None:
            raise ValidationError('Enter a grade and class.')
        return value


class TeacherSubjectSetupForm(forms.Form):
    school_class = forms.ModelChoiceField(
        queryset=SchoolClass.objects.none(),
        label='Class',
    )
    name = forms.CharField(max_length=Subject._meta.get_field('name').max_length)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['school_class'].queryset = SchoolClass.objects.filter(
            teacher_setups__teacher__teacher=user).order_by(
                'grade_number', 'class_letter')
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'


class TeacherRequestFilterForm(forms.Form):
    school_class = forms.ModelChoiceField(
        queryset=SchoolClass.objects.none(),
        required=False,
        label='Class',
        empty_label='All classes',
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['school_class'].queryset = SchoolClass.objects.filter(
            teacher_setups__teacher__teacher=user).order_by(
                'grade_number', 'class_letter')
        self.fields['school_class'].widget.attrs['class'] = 'form-control'


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
    grade_and_class = forms.CharField(
        label='Grade and class',
        help_text='Examples: 8A, 8 a, 9',
    )
    gender = forms.ChoiceField(
        choices=CustomUser.GENDER,
        widget=forms.RadioSelect,
    )
    password = forms.CharField(
        strip=False,
        min_length=8,
        error_messages={
            'min_length': 'Password must be at least 8 characters long.',
        },
        help_text='At least 8 characters. Any characters are allowed.',
        widget=forms.PasswordInput(attrs={
            'minlength': '8',
            'autocomplete': 'new-password',
            'aria-describedby': 'password-length-feedback',
        }),
    )
    confirm_password = forms.CharField(
        label='Confirm password',
        strip=False,
        widget=forms.PasswordInput(attrs={
            'autocomplete': 'new-password',
            'aria-describedby': 'password-match-feedback',
        }),
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

    def clean_grade_and_class(self):
        value = self.cleaned_data['grade_and_class']
        try:
            parse_class_label(value)
        except ClassLabelError as error:
            raise forms.ValidationError(str(error)) from error
        return value

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
        return cleaned_data


class LearnerRegistrationForm(RegistrationForm):
    pass


class TeacherAccountForm(forms.Form):
    first_name = forms.CharField(max_length=150, label='First name')
    last_name = forms.CharField(max_length=150, label='Last name')
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
            self.fields['gender'].initial = user.gender
        self.fields['profile_pic'].help_text = (
            'Optional. JPG, JPEG, or PNG; maximum file size 5 MB.')
        for name, field in self.fields.items():
            if name != 'gender':
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
            raise forms.ValidationError('Choose a JPG, JPEG, or PNG image.')
        if getattr(picture.image, 'format', '').upper() not in ('JPEG', 'PNG'):
            raise forms.ValidationError(
                'The uploaded file must be a valid JPG, JPEG, or PNG image.')
        return picture

class ProfileUpdateForm(forms.Form):
    first_name = forms.CharField(max_length=150, label='First name')
    last_name = forms.CharField(max_length=150, label='Last name')
    grade_and_class = forms.CharField(
        required=False,
        label='Grade and class',
        help_text='Examples: 8A, 8 a, 9',
    )
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

    def __init__(self, *args, user, student=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.fields['first_name'].initial = user.first_name
        self.fields['last_name'].initial = user.last_name
        if user.user_type == '2':
            school_class = getattr(student, 'school_class', None)
            if school_class and school_class.grade_number is not None:
                self.fields['grade_and_class'].initial = (
                    f'{school_class.grade_number}'
                    f'{school_class.class_letter.upper()}'
                )
        else:
            self.fields.pop('grade_and_class')
        self.fields['profile_pic'].help_text = (
            'Optional. JPG, JPEG, or PNG; maximum file size 5 MB.')
        if user.user_type == '1':
            for name in (
                'current_password',
                'new_password',
                'confirm_password',
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
        if user.user_type in ('1', '3'):
            self.fields['reset_pin'].label = 'Reset my PIN'
            self.fields['reset_pin'].help_text = (
                'Enter your current PIN to verify your identity. A new PIN '
                'will be generated and shown once.')
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

    def clean_grade_and_class(self):
        value = self.cleaned_data.get('grade_and_class')
        if value is None:
            return value
        try:
            parse_class_label(value)
        except ClassLabelError as error:
            raise forms.ValidationError(str(error)) from error
        return value

    def clean(self):
        cleaned_data = super().clean()
        if self.user.user_type in ('1', '3'):
            if not cleaned_data.get('reset_pin'):
                return cleaned_data
            current_pin = cleaned_data.get('current_pin')
            if not self.user.pin_hash:
                self.add_error(
                    'current_pin',
                    (
                        'No current PIN is set.'
                        if self.user.user_type == '1'
                        else 'No current PIN is set. Ask an administrator to issue one.'
                    ))
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


class SecurityQuestionUpdateForm(forms.Form):
    question_choice = forms.ChoiceField(
        label='Security question',
        choices=(('', 'Choose a security question'),) + tuple(
            (question, question) for question in SECURITY_QUESTIONS
        ) + (('custom', 'Write my own question'),),
    )
    custom_question = forms.CharField(
        required=False,
        max_length=255,
        label='Your question',
    )
    answer = forms.CharField(
        max_length=255,
        label='Answer',
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
    )
    current_password = forms.CharField(
        required=False,
        label='Current password',
        widget=forms.PasswordInput,
    )
    current_pin = forms.CharField(
        required=False,
        label='Current PIN',
        widget=forms.PasswordInput,
    )

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        saved_question = user.security_question
        if saved_question in SECURITY_QUESTIONS:
            self.fields['question_choice'].initial = saved_question
        elif saved_question:
            self.fields['question_choice'].initial = 'custom'
            self.fields['custom_question'].initial = saved_question

        if user.user_type in ('1', '3'):
            self.fields.pop('current_password')
            self.fields['current_pin'].label = 'Current PIN'
        else:
            self.fields.pop('current_pin')

        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'

    def clean_answer(self):
        answer = normalize_security_answer(self.cleaned_data.get('answer'))
        if len(answer) < 4:
            raise forms.ValidationError(
                'The answer must contain at least 4 characters.')
        return answer

    def clean(self):
        cleaned_data = super().clean()
        choice = cleaned_data.get('question_choice')
        if choice == 'custom':
            question = (cleaned_data.get('custom_question') or '').strip()
            if not question:
                self.add_error('custom_question', 'Enter your question.')
        elif choice:
            question = choice
        else:
            question = ''
            self.add_error(
                'question_choice', 'Choose or write a security question.')

        if self.user.user_type in ('1', '3'):
            current_pin = cleaned_data.get('current_pin')
            if not current_pin or not verify_pin(
                current_pin, self.user.pin_hash
            ):
                self.add_error('current_pin', 'Current PIN is incorrect.')
        else:
            current_password = cleaned_data.get('current_password')
            if (
                not current_password
                or not self.user.check_password(current_password)
            ):
                self.add_error(
                    'current_password', 'Current password is incorrect.')

        cleaned_data['security_question'] = question
        return cleaned_data


class SecurityResetLookupForm(forms.Form):
    first_name = forms.CharField(
        max_length=150,
        label='First name',
    )
    last_name = forms.CharField(
        max_length=150,
        label='Last name',
    )
    grade_and_class = forms.CharField(
        required=False,
        max_length=30,
        label='Grade and class',
        help_text='Examples: 8A, 8 a, 9',
    )
    account_id = forms.CharField(
        required=False,
        max_length=160,
        label='Account ID',
    )

    def __init__(
        self,
        *args,
        role,
        account_id_required=False,
        account_id_only=False,
        **kwargs
    ):
        self.role = role
        super().__init__(*args, **kwargs)
        if account_id_only:
            for name in ('first_name', 'last_name', 'grade_and_class'):
                self.fields.pop(name)
            self.fields['account_id'].required = True
        elif role == 'learner':
            self.fields['grade_and_class'].required = True
            if account_id_required:
                self.fields['account_id'].required = True
            else:
                self.fields.pop('account_id')
        else:
            self.fields.pop('grade_and_class')
            self.fields['account_id'].required = True

        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'

    def clean_grade_and_class(self):
        value = self.cleaned_data.get('grade_and_class')
        try:
            parsed = parse_class_label(value)
        except ClassLabelError as error:
            raise forms.ValidationError(str(error)) from error
        if parsed is None:
            raise forms.ValidationError('Enter a valid class.')
        return parsed


class SecurityResetAnswerForm(forms.Form):
    answer = forms.CharField(
        required=False,
        max_length=255,
        label='Answer',
        widget=forms.PasswordInput(attrs={
            'autocomplete': 'off',
            'class': 'form-control',
        }),
    )

    def clean_answer(self):
        return normalize_security_answer(self.cleaned_data.get('answer'))


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
