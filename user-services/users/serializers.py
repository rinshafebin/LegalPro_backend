# users/serializers.py
from rest_framework import serializers
from django.core.validators import RegexValidator
from django.contrib.auth import get_user_model
from .models import ClientProfile, AdvocateProfile, Specialization

User = get_user_model()


class UserRegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)
    confirm_password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ["email", "password", "confirm_password"]

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("Email already exists")
        return value

    def validate(self, data):
        if data["password"] != data["confirm_password"]:
            raise serializers.ValidationError("Passwords do not match")
        return data

    def create(self, validated_data):
        validated_data.pop("confirm_password")
        user = User.objects.create_user(email=validated_data["email"], password=validated_data["password"], role="client")
        ClientProfile.objects.create(user=user, full_name=user.email)
        return user


class AdvocateRegisterSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)
    password = serializers.CharField(write_only=True)
    confirm_password = serializers.CharField(write_only=True)
    full_name = serializers.CharField(required=True)
    bar_council_id = serializers.CharField(
        validators=[RegexValidator(r"^[A-Za-z0-9]+$", "Bar council ID must be alphanumeric")]
    )

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("Email already exists")
        return value

    def validate_bar_council_id(self, value):
        if AdvocateProfile.objects.filter(bar_council_id=value).exists():
            raise serializers.ValidationError("Bar council ID already exists")
        return value

    def validate(self, data):
        if data["password"] != data["confirm_password"]:
            raise serializers.ValidationError("Passwords do not match")
        return data

    def create(self, validated_data):
        validated_data.pop("confirm_password")
        bar_council_id = validated_data.pop("bar_council_id")
        full_name = validated_data.pop("full_name")

        user = User.objects.create_user(email=validated_data["email"], password=validated_data["password"], role="advocate")
        AdvocateProfile.objects.create(user=user, bar_council_id=bar_council_id, full_name=full_name)
        return user


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, data):
        from django.contrib.auth import authenticate
        # authenticate expects username kwarg, but USERNAME_FIELD is email — pass as username
        user = authenticate(username=data["email"], password=data["password"])
        if not user:
            raise serializers.ValidationError("Invalid email or password")
        data["user"] = user
        return data


class ClientProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClientProfile
        fields = ["full_name", "phone", "address_line1", "address_line2", "city", "state", "pincode", "profile_image"]


class AdvocateProfileSerializer(serializers.ModelSerializer):
    specializations = serializers.ListField(child=serializers.CharField(), required=False)

    class Meta:
        model = AdvocateProfile
        fields = [
            "full_name", "phone", "gender", "dob", "bar_council_id", "enrollment_year",
            "experience_years", "languages", "specializations",
            "address_line1", "address_line2", "city", "state", "pincode", "profile_image"
        ]

    def update(self, instance, validated_data):
        # Handle specializations list
        specs = validated_data.pop("specializations", [])
        for attr, val in validated_data.items():
            setattr(instance, attr, val)
        instance.save()
        if specs:
            for name in specs:
                spec, _ = Specialization.objects.get_or_create(name=name)
                instance.specializations.add(spec)
        return instance


class ForgetPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        if not User.objects.filter(email=value).exists():
            raise serializers.ValidationError("No user found with this email")
        return value


class ResetPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp = serializers.CharField()
    new_password = serializers.CharField(write_only=True)

    def validate_new_password(self, value):
        if len(value) < 6:
            raise serializers.ValidationError("Password must be at least 6 characters")
        if not any(c.isupper() for c in value):
            raise serializers.ValidationError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in value):
            raise serializers.ValidationError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in value):
            raise serializers.ValidationError("Password must contain at least one number")
        return value
