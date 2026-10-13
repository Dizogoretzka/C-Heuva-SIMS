from django.db import migrations


def ensure_role_profiles(apps, schema_editor):
    CustomUser = apps.get_model('main_app', 'CustomUser')
    Admin = apps.get_model('main_app', 'Admin')
    Student = apps.get_model('main_app', 'Student')
    database = schema_editor.connection.alias

    for user in CustomUser.objects.using(database).all().iterator():
        if user.user_type == '1':
            Admin.objects.using(database).get_or_create(admin_id=user.pk)
        elif user.user_type == '2':
            Student.objects.using(database).get_or_create(admin_id=user.pk)


class Migration(migrations.Migration):

    dependencies = [
        ('main_app', '0002_remove_legacy_role_models'),
    ]

    operations = [
        migrations.RunPython(ensure_role_profiles, migrations.RunPython.noop),
    ]
