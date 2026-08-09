from django.urls import path

from . import views

app_name = "pos"

urlpatterns = [
    path("sw.js", views.service_worker, name="sw"),
    path("", views.index, name="index"),
    path("rates/refresh/", views.refresh_rates, name="refresh_rates"),
    path("rates/edit/", views.rate_edit, name="rate_edit"),
    path("rates/save/", views.rate_save, name="rate_save"),
    path("today/", views.today, name="today"),
    path("clients/", views.clients, name="clients"),
    path("clients/debtors/", views.debtors, name="debtors"),
    path("clients/<int:pk>/", views.client_detail, name="client_detail"),
    path("clients/<int:pk>/statement/pdf/", views.statement_download, name="statement_download"),
    path("clients/<int:pk>/statement/send/", views.send_statement, name="send_statement"),
    path(
        "clients/<int:pk>/remind/toggle/",
        views.toggle_do_not_remind,
        name="toggle_do_not_remind",
    ),
    path(
        "clients/<int:pk>/opening-balance/add/",
        views.opening_balance_add,
        name="opening_balance_add",
    ),
    path("clients/<int:pk>/debt/pay/", views.client_debt_pay, name="client_debt_pay"),
    path(
        "clients/<int:pk>/debt/pay/confirm/",
        views.client_debt_pay_confirm,
        name="client_debt_pay_confirm",
    ),
    # The empty terminal — no SaleOrder exists yet (see views.index/sale_new).
    # "new" never collides with <int:pk> below (the int converter only
    # matches digits), but these are still listed first for readability.
    path("sale/new/", views.sale_new, name="sale_new"),
    path("sale/new/clients/search/", views.client_search_new, name="client_search_new"),
    path("sale/new/clients/new/", views.client_new_form_new, name="client_new_form_new"),
    path(
        "sale/new/clients/reset/", views.client_section_reset_new, name="client_section_reset_new"
    ),
    path("sale/new/clients/create/", views.client_create_new, name="client_create_new"),
    path("sale/new/client/<int:client_id>/set/", views.client_set_new, name="client_set_new"),
    path("sale/new/products/", views.product_grid_new, name="product_grid_new"),
    path(
        "sale/new/products/<int:product_id>/variants/",
        views.variant_picker_new,
        name="variant_picker_new",
    ),
    path("sale/new/items/add/", views.item_add_new, name="item_add_new"),
    path("sale/<int:pk>/", views.sale_detail, name="sale_detail"),
    path("sale/<int:pk>/clients/search/", views.client_search, name="client_search"),
    path("sale/<int:pk>/clients/new/", views.client_new_form, name="client_new_form"),
    path("sale/<int:pk>/clients/create/", views.client_create, name="client_create"),
    path("sale/<int:pk>/client/<int:client_id>/set/", views.client_set, name="client_set"),
    path("sale/<int:pk>/client/clear/", views.client_clear, name="client_clear"),
    path("sale/<int:pk>/products/", views.product_grid, name="product_grid"),
    path(
        "sale/<int:pk>/products/<int:product_id>/variants/",
        views.variant_picker,
        name="variant_picker",
    ),
    path("sale/<int:pk>/items/add/", views.item_add, name="item_add"),
    path("sale/<int:pk>/items/<int:item_id>/remove/", views.item_remove, name="item_remove"),
    path("sale/<int:pk>/undo/", views.sale_undo, name="sale_undo"),
    path("sale/<int:pk>/redo/", views.sale_redo, name="sale_redo"),
    path("sale/<int:pk>/items/<int:item_id>/discount/", views.item_discount, name="item_discount"),
    path("sale/<int:pk>/recalc/", views.recalc, name="recalc"),
    path("sale/<int:pk>/confirm/", views.sale_confirm, name="sale_confirm"),
    path("sale/<int:pk>/result/", views.sale_result, name="sale_result"),
    path("sale/<int:pk>/cancel/", views.sale_cancel, name="sale_cancel"),
    path("sale/<int:pk>/return/", views.sale_return, name="sale_return"),
    path("sale/<int:pk>/receipt/", views.share_receipt, name="share_receipt"),
    path("sale/<int:pk>/receipt/pdf/", views.receipt_download, name="receipt_download"),
    path("sale/<int:pk>/debt/", views.debt_pay_detail, name="debt_pay_detail"),
    path("sale/<int:pk>/debt/recalc/", views.debt_pay_recalc, name="debt_pay_recalc"),
    path("sale/<int:pk>/debt/confirm/", views.debt_pay_confirm, name="debt_pay_confirm"),
    path("clients/<int:pk>/remind/", views.debt_reminder, name="debt_reminder"),
]
