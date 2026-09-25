from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("entrar/", auth_views.LoginView.as_view(template_name="dashboard/login.html",
                                                  redirect_authenticated_user=True), name="login"),
    path("sair/", auth_views.LogoutView.as_view(), name="logout"),
    path("empresa/", views.select_company, name="select_company"),
    path("fila/processar/", views.run_queue, name="run_queue"),
    path("faturas/", views.invoice_list, name="invoices"),
    path("faturas/<int:pk>/", views.invoice_detail, name="invoice"),
    path("faturas/<int:pk>/<slug:action>/", views.invoice_action, name="invoice_action"),
    path("origens/", views.source_list, name="sources"),
    path("origens/nova/", views.source_form, name="source_new"),
    path("origens/<int:pk>/editar/", views.source_form, name="source_edit"),
    path("origens/<int:pk>/sincronizar/", views.source_sync, name="source_sync"),
    path("origens/<int:pk>/testar/", views.source_test, name="source_test"),
    path("agt/", views.agt_config, name="agt"),
    path("chaves-api/", views.api_keys, name="api_keys"),
    path("chaves-api/<int:pk>/revogar/", views.api_key_revoke, name="api_key_revoke"),
    path("auditoria/", views.audit_log, name="audit"),
]
