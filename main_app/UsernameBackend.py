from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class UsernameBackend(ModelBackend):
    def get_user_for_identifier(self, username):
        user_model = get_user_model()
        if not isinstance(username, str):
            return None
        username = username.strip()
        if not username:
            return None

        try:
            return user_model._default_manager.get(username__iexact=username)
        except user_model.DoesNotExist:
            if not getattr(settings, 'SIMS_ALLOW_EMAIL_SIGNIN', True) or '@' not in username:
                return None
            try:
                return user_model._default_manager.get(
                    email__iexact=username,
                    username__isnull=True,
                )
            except user_model.DoesNotExist:
                return None

    def authenticate(self, request, username=None, password=None, **kwargs):
        user_model = get_user_model()
        if username is None:
            username = kwargs.get(user_model.USERNAME_FIELD)
        if not isinstance(username, str) or password is None:
            user_model().set_password(password)
            return None

        user = self.get_user_for_identifier(username)
        if user is None:
            user_model().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
