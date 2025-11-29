# users/views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.contrib.auth import authenticate, get_user_model
import pyotp
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from django.conf import settings

from .serializers import (
    UserRegisterSerializer, AdvocateRegisterSerializer, LoginSerializer,
    ClientProfileSerializer, AdvocateProfileSerializer
)
from .tasks import send_welcome_email_task

User = get_user_model()


def custom_response(message, status_code=200, status_type="success", data=None):
    response = {"status": status_type, "code": status_code, "message": message}
    if data:
        response["data"] = data
    return Response(response, status=status_code)


class UserRegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = UserRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        # send welcome email with email and full name (client profile)
        full_name = getattr(user.client_profile, "full_name", user.email)
        send_welcome_email_task.delay(user.email, full_name)
        return custom_response("User registered successfully", status_code=201, data={"user_id": user.id})


class AdvocateRegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        # Accept either single object or {"advocates": [...]} bulk payload
        payload = request.data
        created = []

        if isinstance(payload, dict) and "advocates" in payload:
            adv_list = payload["advocates"]
        elif isinstance(payload, list):
            adv_list = payload
        else:
            adv_list = [payload]

        for adv_data in adv_list:
            serializer = AdvocateRegisterSerializer(data=adv_data)
            serializer.is_valid(raise_exception=True)
            user = serializer.save()
            created.append(user)
            # send welcome email (use advocate profile full_name)
            full_name = getattr(user.advocate_profile, "full_name", user.email)
            send_welcome_email_task.delay(user.email, full_name)

        user_ids = [u.id for u in created]
        return custom_response("Advocate(s) registered successfully", status_code=201, data={"user_ids": user_ids})


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]

        if user.mfa_enabled:
            return custom_response("MFA required", status_code=200, data={"user_id": user.id, "mfa_type": user.mfa_type})

        return custom_response("Login successful", status_code=200, data={"user_id": user.id, "role": user.role})


class GoogleLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        token = request.data.get("token")
        if not token:
            return custom_response("Google token required", status_code=400, status_type="error")
        try:
            info = id_token.verify_oauth2_token(token, google_requests.Request(), settings.GOOGLE_CLIENT_ID)
        except ValueError:
            return custom_response("Invalid Google token", status_code=400, status_type="error")

        email = info.get("email")
        if not email:
            return custom_response("Google account has no email", status_code=400, status_type="error")

        user, created = User.objects.get_or_create(email=email, defaults={"role": "client"})
        if created:
            # create client profile
            from .models import ClientProfile
            ClientProfile.objects.create(user=user, full_name=info.get("name") or email.split("@")[0])
            send_welcome_email_task.delay(user.email, getattr(user, "client_profile").full_name)
        return custom_response("Google login successful", data={"user_id": user.id, "role": user.role})


class EnableMFAView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        if user.role not in ["admin", "advocate"]:
            return custom_response("MFA allowed only for admin/advocate", status_code=403, status_type="error")
        if user.mfa_enabled:
            return custom_response("MFA already enabled", status_code=400, status_type="error")
        secret = pyotp.random_base32()
        user.mfa_secret = secret
        user.mfa_type = "TOTP"
        user.mfa_enabled = True
        user.save()
        totp_uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="YourAppName")
        return custom_response("MFA enabled", data={"mfa_type": "TOTP", "totp_uri": totp_uri})


class VerifyMFAView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        user_id = request.data.get("user_id")
        otp = request.data.get("otp")
        if not user_id or not otp:
            return custom_response("Missing fields", status_code=400, status_type="error")
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return custom_response("User not found", status_code=404, status_type="error")
        if not user.mfa_enabled:
            return custom_response("MFA not enabled, login successful")
        totp = pyotp.TOTP(user.mfa_secret)
        if not totp.verify(otp):
            return custom_response("Invalid OTP", status_code=400, status_type="error")
        return custom_response("MFA verified", data={"user_id": user.id, "role": user.role})


class ClientProfileUpdateView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request):
        if request.user.role != "client":
            return custom_response("Not a client", status_code=403, status_type="error")
        serializer = ClientProfileSerializer(request.user.client_profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return custom_response("Profile updated successfully")


class AdvocateProfileUpdateView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request):
        if request.user.role != "advocate":
            return custom_response("Not an advocate", status_code=403, status_type="error")
        serializer = AdvocateProfileSerializer(request.user.advocate_profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return custom_response("Profile updated successfully")
