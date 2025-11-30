from django.db import models


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

    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "advocate_profile"
        ordering = ["full_name"]



class Case(models.Model):
    id = models.IntegerField(primary_key=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    advocate_id = models.IntegerField()
    client_id = models.IntegerField()
    status = models.CharField(max_length=50)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False  
        db_table = "cases"
