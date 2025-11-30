from rest_framework import serializers
from .models import AdvocateProfile, Specialization

class SpecializationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Specialization
        fields = ["id", "name"]

class AdvocateSerializer(serializers.ModelSerializer):
    specializations = SpecializationSerializer(many=True)

    class Meta:
        model = AdvocateProfile
        fields = [
            "id",
            "full_name",
            "city",
            "state",
            "experience_years",
            "rating",
            "specializations",
        ]


class AdvocateDetailSerializer(serializers.ModelSerializer):
    specializations = SpecializationSerializer(many=True, read_only=True)

    class Meta:
        model = AdvocateProfile
        fields = [
            "id",
            "full_name",
            "phone",
            "gender",
            "dob",
            "bar_council_id",
            "enrollment_year",
            "experience_years",
            "languages",
            "city",
            "state",
            "address_line1",
            "address_line2",
            "pincode",
            "profile_image",
            "is_verified",
            "rating",
            "cases_count",
            "wins_count",
            "created_at",
            "updated_at",
            "specializations",
        ]



class CaseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    title = serializers.CharField()
    description = serializers.CharField()
    advocate_id = serializers.IntegerField()
    client_id = serializers.IntegerField()
    status = serializers.CharField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
