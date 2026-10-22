"""student_management_system URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/3.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.http import Http404
from django.contrib import admin
from django.conf import settings as django_settings
from django.urls import include, path, re_path
from django.conf.urls.static import static
from django.views.generic.base import RedirectView
from django.contrib.staticfiles.views import serve as serve_static
from . import settings


def unavailable_admin_password_change(request):
    raise Http404


def serve_static_asset(request, path):
    if not django_settings.DEBUG:
        raise Http404
    return serve_static(request, path, insecure=True)


urlpatterns = [
    path(
        'favicon.ico',
        RedirectView.as_view(
            url=f'{settings.STATIC_URL.rstrip("/")}/main_app/img/favicon.ico',
            permanent=True,
        ),
        name='favicon',
    ),
    path(
        'admin/password_change/done/',
        unavailable_admin_password_change,
    ),
    path(
        'admin/password_change/',
        unavailable_admin_password_change,
    ),
    path("", include('main_app.urls')),
    path('admin/', admin.site.urls),
]

urlpatterns.append(re_path(
    r'^static/(?P<path>.*)$',
    serve_static_asset,
    name='static_asset',
))

urlpatterns += (
    static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    + static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
)
