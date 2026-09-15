from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from workflow_target import views

urlpatterns = [
    path(
        "_workflow/session/",
        views.session_landing,
        name="workflow_session_landing",
    ),
    path(
        "_workflow/session/consume/",
        views.consume_session,
        name="workflow_session_consume",
    ),
    path("admin/", admin.site.urls),
    path("", include("helpdesk.urls", namespace="helpdesk")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)