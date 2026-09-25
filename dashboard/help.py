"""Área de ajuda: guias simples para ligar uma base de dados ou um sistema por API."""

from django.shortcuts import render
from django.urls import reverse

from .access import company_view


@company_view()
def help_index(request):
    return render(request, "dashboard/help/index.html")


@company_view()
def help_database(request):
    return render(request, "dashboard/help/database.html")


@company_view()
def help_api(request):
    return render(request, "dashboard/help/api.html", {
        "endpoint": request.build_absolute_uri(reverse("api:documents")),
        "ping": request.build_absolute_uri(reverse("api:ping")),
    })
