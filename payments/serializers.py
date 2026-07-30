from rest_framework import serializers

from .models import Earning, Withdrawal


class EarningSerializer(serializers.ModelSerializer):
    project_code = serializers.CharField(source="project.code", read_only=True)
    project_title = serializers.CharField(source="project.title", read_only=True)
    project_id = serializers.IntegerField(source="project.id", read_only=True)
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = Earning
        fields = [
            "id", "project_id", "project_code", "project_title",
            "kind", "kind_label", "share_percent", "amount_usd", "created_at",
        ]


class WithdrawalSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    masked_account = serializers.CharField(read_only=True)
    user_name = serializers.SerializerMethodField()
    user_role = serializers.CharField(source="user.role_label", read_only=True)

    class Meta:
        model = Withdrawal
        fields = [
            "id", "reference", "amount_usd", "currency", "amount_subunit",
            "usd_to_ngn_rate", "bank_name", "bank_account_name", "masked_account",
            "status", "status_label", "note", "processed_at", "created_at",
            "user_name", "user_role",
        ]

    def get_user_name(self, obj):
        return obj.user.full_name or obj.user.email


class WithdrawalCreateSerializer(serializers.Serializer):
    amount_usd = serializers.DecimalField(max_digits=12, decimal_places=2)
    bank_name = serializers.CharField(max_length=120)
    bank_account_number = serializers.CharField(max_length=34)
    bank_account_name = serializers.CharField(max_length=150)


class WithdrawalSettleSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=[
            Withdrawal.Status.PROCESSING,
            Withdrawal.Status.PAID,
            Withdrawal.Status.REJECTED,
        ]
    )
    note = serializers.CharField(required=False, allow_blank=True, default="")
