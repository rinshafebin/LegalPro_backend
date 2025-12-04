# users/views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import RefreshToken
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


def get_tokens_for_user(user):
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


# ------------------------- Registration Views -------------------------
class UserRegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = UserRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        full_name = getattr(user.client_profile, "full_name", user.email)
        send_welcome_email_task.delay(user.email, full_name)
        return custom_response("User registered successfully", 201, data={"user_id": user.id})


class AdvocateRegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        payload = request.data
        advocates = payload.get("advocates") if isinstance(payload, dict) else (payload if isinstance(payload, list) else [payload])
        created_users = []

        for adv_data in advocates:
            serializer = AdvocateRegisterSerializer(data=adv_data)
            serializer.is_valid(raise_exception=True)
            user = serializer.save()
            created_users.append(user)
            full_name = getattr(user.advocate_profile, "full_name", user.email)
            send_welcome_email_task.delay(user.email, full_name)

        return custom_response("Advocate(s) registered successfully", 201, data={"user_ids": [u.id for u in created_users]})


# ------------------------- Login Views -------------------------
class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]

        if user.mfa_enabled:
            # MFA required, return minimal info
            return custom_response("MFA required", data={"user_id": user.id, "mfa_type": user.mfa_type})

        tokens = get_tokens_for_user(user)
        return custom_response("Login successful", data={"user_id": user.id, "role": user.role, "tokens": tokens})


class GoogleLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        token = request.data.get("token")
        if not token:
            return custom_response("Google token required", 400, "error")
        try:
            info = id_token.verify_oauth2_token(token, google_requests.Request(), settings.GOOGLE_CLIENT_ID)
        except ValueError:
            return custom_response("Invalid Google token", 400, "error")

        email = info.get("email")
        if not email:
            return custom_response("Google account has no email", 400, "error")

        user, created = User.objects.get_or_create(email=email, defaults={"role": "client"})
        if created:
            from .models import ClientProfile
            ClientProfile.objects.create(user=user, full_name=info.get("name") or email.split("@")[0])
            send_welcome_email_task.delay(user.email, getattr(user, "client_profile").full_name)

        if user.mfa_enabled:
            return custom_response("MFA required", data={"user_id": user.id, "mfa_type": user.mfa_type})

        tokens = get_tokens_for_user(user)
        return custom_response("Google login successful", data={"user_id": user.id, "role": user.role, "tokens": tokens})


# ------------------------- MFA Views -------------------------
class EnableMFAView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        if user.role not in ["admin", "advocate"]:
            return custom_response("MFA allowed only for admin/advocate", 403, "error")
        if user.mfa_enabled:
            return custom_response("MFA already enabled", 400, "error")

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
            return custom_response("Missing fields", 400, "error")
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return custom_response("User not found", 404, "error")
        if not user.mfa_enabled:
            return custom_response("MFA not enabled, login successful")

        totp = pyotp.TOTP(user.mfa_secret)
        if not totp.verify(otp):
            return custom_response("Invalid OTP", 400, "error")

        tokens = get_tokens_for_user(user)
        return custom_response("MFA verified", data={"user_id": user.id, "role": user.role, "tokens": tokens})


# ------------------------- Profile Update Views -------------------------
class ClientProfileUpdateView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request):
        if request.user.role != "client":
            return custom_response("Not a client", 403, "error")
        serializer = ClientProfileSerializer(request.user.client_profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return custom_response("Profile updated successfully")


class AdvocateProfileUpdateView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request):
        if request.user.role != "advocate":
            return custom_response("Not an advocate", 403, "error")
        serializer = AdvocateProfileSerializer(request.user.advocate_profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return custom_response("Profile updated successfully")
