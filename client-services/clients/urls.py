from django.urls import path
from .views import (
    AppointmentCreateView,
    AppointmentListView,
    CreateOrderView,
    VerifyPaymentView,
    CaseListView,
    CaseCreatedEvent,
)

urlpatterns = [
    path("appointments/create/", AppointmentCreateView.as_view()),
    path("appointments/", AppointmentListView.as_view()),
    
    path("payments/order/", CreateOrderView.as_view()),
    path("payments/verify/", VerifyPaymentView.as_view()),

    path("cases/", CaseListView.as_view()),

    path("events/case-created/", CaseCreatedEvent.as_view()),
]
