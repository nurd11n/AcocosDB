from django.urls import path

from . import views

app_name = "manufacturing"

urlpatterns = [
    path("", views.contractors, name="contractors"),
    path("contractors/<int:pk>/", views.contractor_detail, name="contractor_detail"),
    path(
        "contractors/<int:pk>/transaction/add/",
        views.contractor_transaction_add,
        name="contractor_transaction_add",
    ),
    path("production/add/", views.production_add, name="production_add"),
    path("production/search/", views.production_search, name="production_search"),
    path(
        "production/grid/<int:product_id>/",
        views.production_grid_view,
        name="production_grid",
    ),
    path("expenses/", views.expenses, name="expenses"),
    path("dashboard/", views.manufacturing_dashboard, name="dashboard"),
]
