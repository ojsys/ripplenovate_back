from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import User


class UserSerializer(serializers.ModelSerializer):
    initials = serializers.CharField(read_only=True)
    role_label = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = [
            "id", "email", "full_name", "role", "role_label",
            "company", "specialty", "active_load",
            "is_email_verified", "initials",
        ]
        read_only_fields = ["id", "role", "is_email_verified", "active_load"]


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])

    class Meta:
        model = User
        fields = ["email", "full_name", "company", "password"]

    def validate_email(self, value):
        value = value.lower().strip()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def create(self, validated_data):
        # Public signup always creates a Client; roles are elevated by an admin.
        return User.objects.create_user(role=User.Role.CLIENT, **validated_data)


class ProfileUpdateSerializer(serializers.ModelSerializer):
    """A user editing their own profile."""

    class Meta:
        model = User
        fields = ["full_name", "company", "specialty"]

    def validate_full_name(self, v):
        if not v.strip():
            raise serializers.ValidationError("Your name can't be empty.")
        return v.strip()


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField()
    new_password = serializers.CharField(validators=[validate_password])


class DeveloperCreateSerializer(serializers.ModelSerializer):
    """A delivery lead creating a developer account + profile."""

    password = serializers.CharField(write_only=True, validators=[validate_password])

    class Meta:
        model = User
        fields = ["id", "email", "full_name", "specialty", "password"]

    def validate_email(self, value):
        value = value.lower().strip()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def create(self, validated_data):
        # Lead-created developers are active immediately (no email verification step).
        return User.objects.create_user(
            role=User.Role.DEVELOPER, is_email_verified=True, **validated_data
        )


class DeveloperUpdateSerializer(serializers.ModelSerializer):
    """A delivery lead editing a developer's profile."""

    class Meta:
        model = User
        fields = ["full_name", "specialty", "active_load"]


class RoleUpdateSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=User.Role.choices)
    specialty = serializers.CharField(required=False, allow_blank=True)


class PasswordResetConfirmSerializer(serializers.Serializer):
    token = serializers.UUIDField()
    password = serializers.CharField(validators=[validate_password])
