from rest_framework import serializers

from .models import ProductLine, Service


class ServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Service
        fields = ["id", "name", "description", "typical_timeline"]


class ProductLineSerializer(serializers.ModelSerializer):
    """A line and its offerings — what the client's brief form is built from."""

    services = serializers.SerializerMethodField()

    class Meta:
        model = ProductLine
        fields = ["id", "slug", "name", "tagline", "description",
                  "accent", "icon", "services"]

    def get_services(self, obj):
        # Only live offerings: a retired service stays on old projects but must
        # not be selectable on a new brief.
        active = [s for s in obj.services.all() if s.is_active]
        return ServiceSerializer(active, many=True).data


class ProductLineBriefSerializer(serializers.ModelSerializer):
    """Just enough to label a project — no service list."""

    class Meta:
        model = ProductLine
        fields = ["id", "slug", "name", "accent"]
