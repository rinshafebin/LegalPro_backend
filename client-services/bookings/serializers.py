from rest_framework import serializers
from .models import Booking

class BookingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Booking
        fields = [
            "id",
            "client_id",
            "advocate_id",
            "appointment_datetime",
            "status",
            "created_at",
            "updated_at"
        ]
