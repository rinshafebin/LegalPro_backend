from django.db import models
from django.utils import timezone

# Reference to users table (single DB)
class User(models.Model):
    id = models.AutoField(primary_key=True)
    email = models.EmailField()
    role = models.CharField(max_length=20)

    class Meta:
        managed = False
        db_table = "users"

class Specialization(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100)

    class Meta:
        managed = False
        db_table = "specialization"

class AdvocateProfile(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.OneToOneField(
        User, on_delete=models.DO_NOTHING, related_name="advocate_profile"
    )

    # Personal info
    full_name = models.CharField(max_length=255)
    phone = models.CharField(max_length=20, blank=True, null=True)
    gender = models.CharField(max_length=10, blank=True, null=True)
    dob = models.DateField(blank=True, null=True)

    # Professional info
    bar_council_id = models.CharField(max_length=100)
    enrollment_year = models.IntegerField(blank=True, null=True)
    experience_years = models.IntegerField(default=0)
    languages = models.CharField(max_length=255, blank=True, null=True)
    specializations = models.ManyToManyField(
        Specialization,
        related_name="advocates",
        blank=True
    )

    # Address
    address_line1 = models.CharField(max_length=255, blank=True, null=True)
    address_line2 = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=120, blank=True, null=True)
    state = models.CharField(max_length=120, blank=True, null=True)
    pincode = models.CharField(max_length=20, blank=True, null=True)

    # Profile info
    profile_image = models.CharField(max_length=500, blank=True, null=True)
    is_verified = models.BooleanField(default=False)
    rating = models.FloatField(default=0.0)
    cases_count = models.IntegerField(default=0)
    wins_count = models.IntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "advocate_profile"
        ordering = ["full_name"]




class Case(models.Model):
    STATUS_CHOICES = [
        ('Active', 'Active'),
        ('Pending', 'Pending'),
        ('Closed', 'Closed'),
    ]

    # Minimal fields needed for client view
    id = models.AutoField(primary_key=True)
    title = models.CharField(max_length=255)
    description = models.TextField()
    case_number = models.CharField(max_length=50)
    client_id = models.IntegerField(db_index=True)
    advocate_id = models.IntegerField(db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending', db_index=True)

    result = models.CharField(max_length=20, default='Pending')
    hearing_date = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False  
        db_table = 'case'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({self.status})"
