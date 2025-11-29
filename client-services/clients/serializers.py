from rest_framework import serializers
from .models import Appointment, Payment, Transaction, ExternalCase

class AppointmentCreateSerializer(serializers.Serializer):
    advocate_id = serializers.IntegerField()
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = "__all__"


class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = "__all__"


class ExternalCaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExternalCase
        fields = "__all__"
