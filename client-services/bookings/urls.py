from django.urls import path
from bookings.views import BookingCreateView, BookingListView, BookingDetailView

urlpatterns = [
    path('bookings/', BookingListView.as_view(), name='booking-list'),
    path('bookings/create/', BookingCreateView.as_view(), name='booking-create'),
    path('bookings/<int:booking_id>/', BookingDetailView.as_view(), name='booking-detail'),
]
