from django.urls import path

from . import views

app_name = "api"

urlpatterns = [
    path("ping/", views.PingView.as_view(), name="ping"),
    path("documents/", views.DocumentListView.as_view(), name="documents"),
    path("documents/<str:source_document_id>/", views.DocumentDetailView.as_view(), name="document"),
]
