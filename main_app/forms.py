from django import forms
from django.forms.widgets import DateInput, TextInput

from .models import *


class FormSettings(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super(FormSettings, self).__init__(*args, **kwargs)
        # Here make some changes such as:
        for field in self.visible_fields():
            field.field.widget.attrs['class'] = 'form-control'


class CustomUserForm(FormSettings):
    email = forms.EmailField(required=True)
    gender = forms.ChoiceField(choices=[('M', 'Male'), ('F', 'Female')])
    first_name = forms.CharField(required=True)
    last_name = forms.CharField(required=True)
    address = forms.CharField(widget=forms.Textarea)
    password = forms.CharField(widget=forms.PasswordInput)
    widget = {
        'password': forms.PasswordInput(),
    }
    profile_pic = forms.ImageField()

    def __init__(self, *args, **kwargs):
        super(CustomUserForm, self).__init__(*args, **kwargs)

        if kwargs.get('instance'):
            instance = kwargs.get('instance').admin.__dict__
            self.fields['password'].required = False
            for field in CustomUserForm.Meta.fields:
                self.fields[field].initial = instance.get(field)
            if self.instance.pk is not None:
                self.fields['password'].widget.attrs['placeholder'] = "Fill this only if you wish to update password"

    def clean_email(self, *args, **kwargs):
        formEmail = self.cleaned_data['email'].lower()
        if self.instance.pk is None:  # Insert
            if CustomUser.objects.filter(email=formEmail).exists():
                raise forms.ValidationError(
                    "The given email is already registered")
        else:  # Update
            dbEmail = self.Meta.model.objects.get(
                id=self.instance.pk).admin.email.lower()
            if dbEmail != formEmail:  # There has been changes
                if CustomUser.objects.filter(email=formEmail).exists():
                    raise forms.ValidationError("The given email is already registered")

        return formEmail

    class Meta:
        model = CustomUser
        fields = ['first_name', 'last_name', 'email', 'gender',  'password','profile_pic', 'address' ]


class StudentForm(CustomUserForm):
    def __init__(self, *args, **kwargs):
        super(StudentForm, self).__init__(*args, **kwargs)

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

    def __init__(self, *args, **kwargs):
        super(SubjectForm, self).__init__(*args, **kwargs)

    class Meta:
        model = Subject
        fields = ['name', 'course']


class StudentResultForm(FormSettings):
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['report_type'].widget.attrs['class'] = 'form-control'
        for name in ('course', 'session', 'subject'):
            self.fields[name].widget.attrs['class'] = 'form-control'
        if self.is_bound:
            course_id = self.data.get('course')
            if course_id:
                self.fields['subject'].queryset = Subject.objects.filter(
                    course_id=course_id)

    def clean(self):
        cleaned_data = super().clean()
        report_type = cleaned_data.get('report_type')
        if report_type == 'school_summary':
            return cleaned_data

        if not cleaned_data.get('course'):
            self.add_error('course', 'Choose a course or grade.')
        if not cleaned_data.get('session'):
            self.add_error('session', 'Choose a session or term.')
        return cleaned_data


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
