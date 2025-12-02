from celery import shared_task
from bookings.models import Booking
from django.utils import timezone

@shared_task(name="client_service.tasks.create_booking")
def create_booking(client_id, advocate_id, appointment_datetime):
    booking = Booking.objects.create(
        client_id=client_id,
        advocate_id=advocate_id,
        appointment_datetime=appointment_datetime,
        created_at=timezone.now()
    )

    # MUST RETURN SERIALIZABLE DATA
    return {
        "id": booking.id,
        "client_id": booking.client_id,
        "advocate_id": booking.advocate_id,
        "appointment_datetime": booking.appointment_datetime.isoformat(),
        "status": booking.status,
        "created_at": booking.created_at.isoformat(),
        "updated_at": booking.updated_at.isoformat(),
    }


@shared_task(name="client_service.tasks.get_bookings_by_client")
def get_bookings_by_client(client_id):
    bookings = Booking.objects.filter(client_id=client_id)

    return [
        {
            "id": b.id,
            "client_id": b.client_id,
            "advocate_id": b.advocate_id,
            "appointment_datetime": b.appointment_datetime.isoformat(),
            "status": b.status,
            "created_at": b.created_at.isoformat(),
            "updated_at": b.updated_at.isoformat(),
        }
        for b in bookings
    ]


@shared_task(name="client_service.tasks.get_booking_detail")
def get_booking_detail(booking_id, client_id):
    try:
        b = Booking.objects.get(id=booking_id, client_id=client_id)
        return {
            "id": b.id,
            "client_id": b.client_id,
            "advocate_id": b.advocate_id,
            "appointment_datetime": b.appointment_datetime.isoformat(),
            "status": b.status,
            "created_at": b.created_at.isoformat(),
            "updated_at": b.updated_at.isoformat(),
        }
    except Booking.DoesNotExist:
        return None
