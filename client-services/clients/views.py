import razorpay
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status

from .models import Appointment, Payment, Transaction, ExternalCase
from .serializers import (
    AppointmentCreateSerializer,
    PaymentSerializer,
    ExternalCaseSerializer
)

from .tasks import (
    notify_appointment_created,
    send_payment_event
)

client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))



class AppointmentCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = AppointmentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data

        appointment = Appointment.objects.create(
            client_id=request.user.id,
            advocate_id=data["advocate_id"],
            start_time=data["start_time"],
            end_time=data["end_time"],
        )

        notify_appointment_created.delay(
            appointment.id,
            appointment.advocate_id,
            str(appointment.start_time)
        )

        return Response({"message": "Appointment created", "id": appointment.id}, status=201)



class AppointmentListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        appts = Appointment.objects.filter(client_id=request.user.id)

        data = [
            {
                "id": a.id,
                "advocate_id": a.advocate_id,
                "start_time": a.start_time,
                "end_time": a.end_time,
                "status": a.status,
            }
            for a in appts
        ]

        return Response(data)



class CreateOrderView(APIView):
    def post(self, request):
        amount = int(request.data["amount"])
        user_id = request.data["user_id"]
        case_id = request.data["case_id"]

        order = client.order.create({
            "amount": amount,
            "currency": "INR",
            "payment_capture": 1
        })

        payment = Payment.objects.create(
            order_id=order["id"],
            amount=amount,
            user_id=user_id,
            case_id=case_id
        )

        return Response({
            "order_id": order["id"],
            "payment": PaymentSerializer(payment).data
        })


class VerifyPaymentView(APIView):
    def post(self, request):
        order_id = request.data["order_id"]
        payment_id = request.data["razorpay_payment_id"]
        signature = request.data["razorpay_signature"]

        try:
            client.utility.verify_payment_signature({
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": signature
            })

            payment = Payment.objects.get(order_id=order_id)
            payment.status = "paid"
            payment.razorpay_payment_id = payment_id
            payment.save()

            Transaction.objects.create(
                payment=payment,
                razorpay_payment_id=payment_id,
                razorpay_signature=signature
            )

            send_payment_event.delay(payment.id, payment.case_id, payment.amount)

            return Response({"message": "Payment verified"})

        except:
            payment = Payment.objects.get(order_id=order_id)
            payment.status = "failed"
            payment.save()

            return Response({"error": "Payment failed"}, status=400)



class CaseListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        cases = ExternalCase.objects.filter(client_id=request.user.id)
        return Response(ExternalCaseSerializer(cases, many=True).data)



class CaseCreatedEvent(APIView):
    def post(self, request):
        ExternalCase.objects.create(
            external_id=request.data["case_id"],
            title=request.data["title"],
            advocate_id=request.data["advocate_id"],
            client_id=request.data["client_id"]
        )
        return Response({"message": "Case synced into client-service"})
