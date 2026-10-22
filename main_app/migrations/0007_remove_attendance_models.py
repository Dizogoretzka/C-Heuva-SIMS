from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('main_app', '0006_alter_feedbackstudent_options_and_more'),
    ]

    operations = [
        migrations.DeleteModel(
            name='AttendanceReport',
        ),
        migrations.DeleteModel(
            name='Attendance',
        ),
    ]
