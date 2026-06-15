from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("auth/login/", views.login_view, name="login"),
    path("auth/logout/", views.logout_view, name="logout"),
    path("oauth/callback/", views.auth_callback, name="auth_callback"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("projects/", views.project_list, name="projects"),
    path("projects/<int:project_id>/", views.project_detail, name="project_detail"),
    path("projects/<int:project_id>/edit/", views.edit_project, name="edit_project"),
    path("projects/<int:project_id>/delete/", views.delete_project, name="delete_project"),
    path("projects/<int:project_id>/lock/", views.lock_project, name="lock_project"),
    path("projects/<int:project_id>/unlock/", views.unlock_project, name="unlock_project"),
    path("projects/<int:project_id>/ship/", views.ship_project, name="ship_project"),
    path("explore/", views.explore, name="explore"),
    path("shop/", views.shop, name="shop"),
    path("shop/<int:item_id>", views.item_detail, name="item_detail"),
    path("shop/<int:item_id>/order", views.order_item, name="order_item"),
    path("shop/<int:item_id>/delete/", views.delete_item, name="delete_item"),
    path("shop/<int:item_id>/edit/", views.edit_item, name="edit_item"),
    path("root/shop/create/", views.create_item, name="create_item"),
    path("projects/create/", views.create_project, name="create_project"),
    path("root/", views.admin_dash, name="admin_dash"),
    path("root/fulfillment/", views.fulfillment_dash, name="fulfillment_dash"),
    path("root/fulfillment/<int:order_id>/status/", views.update_order_status, name="update_order_status"),
    path("root/shop/", views.shop_dash, name="shop_dash"),
    path("root/review/", views.review_dash, name="review_dash"),
    path("root/review/<int:ship_id>", views.review_project, name="review_project"),
    path("root/ysws_review/", views.ysws_review_dash, name="ysws_review_dash"),
    path("root/fraud_review/", views.fraud_review_dash, name="fraud_review_dash"),
    path("root/print/", views.print_dash, name="print_dash")
]  