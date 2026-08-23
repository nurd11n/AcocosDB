from django.urls import path

from . import views

app_name = "personal"

urlpatterns = [
    path("", views.expenses, name="expenses"),
    path("export/", views.expenses_export, name="expenses_export"),
]
