from django.urls import path

from . import views

app_name = "orders"

urlpatterns = [
    path("", views.index, name="index"),
    path("queue/", views.queue, name="queue"),
    path("new/", views.create, name="create"),
    path("clients/search/", views.client_search, name="client_search"),
    path("clients/create/", views.client_create, name="client_create"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/due-date/", views.set_due_date, name="set_due_date"),
    path("<int:pk>/step/due/", views.step_due, name="step_due"),
    path("<int:pk>/products/", views.product_grid, name="product_grid"),
    path(
        "<int:pk>/products/<int:product_id>/variants/",
        views.variant_picker,
        name="variant_picker",
    ),
    path("<int:pk>/items/add/", views.item_add, name="item_add"),
    path("<int:pk>/items/<int:item_id>/remove/", views.item_remove, name="item_remove"),
    path("<int:pk>/items/<int:item_id>/produce/", views.produce, name="produce"),
    path("<int:pk>/deposit/", views.deposit_add, name="deposit_add"),
    path("<int:pk>/deliver/confirm/", views.deliver_confirm, name="deliver_confirm"),
    path("<int:pk>/deliver/", views.deliver, name="deliver"),
    path("<int:pk>/cancel/", views.cancel, name="cancel"),
]
