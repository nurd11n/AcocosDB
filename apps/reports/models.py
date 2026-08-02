# No models of its own — this app is the dashboard/report/export machinery
# (dashboard.py, client_export.py, export.py, storage.py, views.py) built on
# top of apps.sales/apps.orders/apps.inventory. It used to also hold
# DailyReview, a proxy Payment admin for the day-end payment-review queue;
# that feature was removed (2026-08) along with this file's only model.
