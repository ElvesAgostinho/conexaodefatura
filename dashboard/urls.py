from django.contrib.auth import views as auth_views
from django.urls import path

from . import connections, views

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
    # Ligações (origens): base de dados ou API
    path("ligacoes/", connections.connection_list, name="connections"),
    path("ligacoes/nova/<slug:kind>/", connections.connection_form, name="connection_new"),
    path("ligacoes/<int:pk>/", connections.connection_detail, name="connection"),
    path("ligacoes/<int:pk>/editar/", connections.connection_form, name="connection_edit"),
    path("ligacoes/<int:pk>/testar/", connections.connection_test, name="connection_test"),
    path("ligacoes/<int:pk>/sincronizar/", connections.connection_sync, name="connection_sync"),
    path("ligacoes/<int:pk>/estrutura/", connections.connection_explore, name="connection_explore"),
    path("ligacoes/<int:pk>/mapeamento/", connections.connection_mapping, name="connection_mapping"),
    path("ligacoes/<int:pk>/mapeamento/json/", connections.connection_mapping_json, name="connection_mapping_json"),
    path("ligacoes/<int:pk>/chaves/", connections.connection_create_key, name="connection_create_key"),
    path("agt/", views.agt_config, name="agt"),
    path("chaves-api/", views.api_keys, name="api_keys"),
    path("chaves-api/<int:pk>/revogar/", views.api_key_revoke, name="api_key_revoke"),
    path("auditoria/", views.audit_log, name="audit"),
]
