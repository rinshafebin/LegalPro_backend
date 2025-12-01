from celery import shared_task
from bookings.models import Booking

@shared_task(name="client_service.tasks.create_booking")
def create_booking(client_id, advocate_id, appointment_datetime):
    booking = Booking.objects.create(
        client_id=client_id,
        advocate_id=advocate_id,
        appointment_datetime=appointment_datetime
    )
    return booking

@shared_task(name="client_service.tasks.get_bookings_by_client")
def get_bookings_by_client(client_id):
    return list(Booking.objects.filter(client_id=client_id))

@shared_task(name="client_service.tasks.get_booking_detail")
def get_booking_detail(booking_id, client_id):
    try:
        return Booking.objects.get(id=booking_id, client_id=client_id)
    except Booking.DoesNotExist:
        return None
