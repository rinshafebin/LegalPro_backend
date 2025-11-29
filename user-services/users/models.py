# users/models.py
from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.utils import timezone
import datetime


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, role="client", **extra_fields):
        if not email:
            raise ValueError("Email must be provided")
        email = self.normalize_email(email)
        user = self.model(email=email, role=role, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("role", "admin")
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    ROLE_CHOICES = (
        ("client", "Client"),
        ("advocate", "Advocate"),
        ("admin", "Admin"),
    )

    email = models.EmailField(unique=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, db_index=True)

    # Django required fields
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    # MFA Fields
    mfa_enabled = models.BooleanField(default=False)
    mfa_type = models.CharField(max_length=10, choices=[("TOTP", "TOTP")], blank=True, null=True)
    mfa_secret = models.CharField(max_length=64, blank=True, null=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    def __str__(self):
        return f"{self.email} ({self.role})"

    class Meta:
        db_table = "users"
        ordering = ["email"]
        indexes = [models.Index(fields=["email"]), models.Index(fields=["role"])]


class Specialization(models.Model):
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name


class ClientProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="client_profile")
    full_name = models.CharField(max_length=100, blank=True, null=True)
    phone = models.CharField(max_length=20, db_index=True, blank=True, null=True)
    address_line1 = models.CharField(max_length=255, blank=True, null=True)
    address_line2 = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=120, blank=True, null=True)
    state = models.CharField(max_length=120, blank=True, null=True)
    pincode = models.CharField(max_length=20, blank=True, null=True)
    profile_image = models.ImageField(upload_to="clients/", blank=True, null=True)

    def __str__(self):
        return self.full_name or self.user.email

    class Meta:
        db_table = "client_profile"
        indexes = [models.Index(fields=["user"]), models.Index(fields=["phone"])]


class AdvocateProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="advocate_profile")

    # Personal info
    full_name = models.CharField(max_length=255)
    phone = models.CharField(max_length=20, blank=True, null=True)
    gender = models.CharField(max_length=10, blank=True, null=True)
    dob = models.DateField(blank=True, null=True)

    # Professional info
    bar_council_id = models.CharField(max_length=100, unique=True)
    enrollment_year = models.IntegerField(blank=True, null=True)
    experience_years = models.IntegerField(default=0)
    languages = models.CharField(max_length=255, blank=True, null=True)
    specializations = models.ManyToManyField(Specialization, related_name="advocates", blank=True)

    # Address info
    address_line1 = models.CharField(max_length=255, blank=True, null=True)
    address_line2 = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=120, blank=True, null=True)
    state = models.CharField(max_length=120, blank=True, null=True)
    pincode = models.CharField(max_length=20, blank=True, null=True)

    # Profile info
    profile_image = models.ImageField(upload_to="advocates/", blank=True, null=True)
    is_verified = models.BooleanField(default=False)
    rating = models.FloatField(default=0.0)
    cases_count = models.IntegerField(default=0)
    wins_count = models.IntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.full_name

    class Meta:
        db_table = "advocate_profile"
        ordering = ["full_name"]
        indexes = [models.Index(fields=["user"]), models.Index(fields=["bar_council_id"]), models.Index(fields=["full_name"])]


class OTP(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    code = models.CharField(max_length=6, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False, db_index=True)

    def is_expired(self):
        return timezone.now() > self.created_at + datetime.timedelta(minutes=5)

    def mark_used(self):
        self.is_used = True
        self.save()

    class Meta:
        db_table = "otp"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user"]), models.Index(fields=["code"])]
