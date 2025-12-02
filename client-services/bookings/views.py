from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from bookings.serializers import BookingSerializer
from client_service.celery import app

class BookingCreateView(APIView):
    def post(self, request):
        data = request.data

        client_id = data["client_id"]

        task = app.send_task(
            "client_service.tasks.create_booking",
            kwargs={
                "client_id": client_id,
                "advocate_id": data["advocate_id"],
                "appointment_datetime": data["appointment_datetime"]
            }
        )

        result = task.get(timeout=20)
        return Response({"booking": result}, status=status.HTTP_201_CREATED)



class BookingListView(APIView):
    def get(self, request):
        client_id = request.GET.get("client_id")

        task = app.send_task(
            "client_service.tasks.get_bookings_by_client",
            kwargs={"client_id": int(client_id)}
        )

        bookings = task.get(timeout=20)
        return Response({"bookings": bookings}, status=status.HTTP_200_OK)



class BookingDetailView(APIView):
    def get(self, request, booking_id):
        client_id = request.GET.get("client_id")

        task = app.send_task(
            "client_service.tasks.get_booking_detail",
            kwargs={"booking_id": int(booking_id), "client_id": int(client_id)}
        )

        booking = task.get(timeout=20)
        if not booking:
            return Response({"error": "Booking not found"}, status=status.HTTP_404_NOT_FOUND)

        return Response({"booking": booking}, status=status.HTTP_200_OK)
