from rest_framework import serializers
from .models import AdvocateProfile, Specialization, Case

class SpecializationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Specialization
        fields = ["id", "name"]

class AdvocateProfileSerializer(serializers.ModelSerializer):
    specializations = SpecializationSerializer(many=True, read_only=True)

    class Meta:
        model = AdvocateProfile
        fields = [
            "id", "full_name", "phone", "gender", "dob",
            "bar_council_id", "enrollment_year", "experience_years", "languages",
            "specializations", "city", "state", "pincode",
            "profile_image", "is_verified", "rating", "cases_count", "wins_count",
            "created_at", "updated_at"
        ]


class CaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Case
        fields = [
            "id",
            "title",
            "description",
            "case_number",
            "advocate_id",
            "status",
            "result",
            "hearing_date",
            "created_at",
            "updated_at"
        ]