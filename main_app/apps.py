from django.apps import AppConfig


class MainAppConfig(AppConfig):
    name = 'main_app'
    default_auto_field = 'django.db.models.AutoField'

    def ready(self):
        from . import activity  # noqa: F401
