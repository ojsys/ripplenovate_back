from rest_framework import serializers

from .models import Earning, Withdrawal


class EarningSerializer(serializers.ModelSerializer):
    project_code = serializers.CharField(source="project.code", read_only=True)
    project_title = serializers.CharField(source="project.title", read_only=True)
    project_id = serializers.IntegerField(source="project.id", read_only=True)
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    # Set on an expert's payment for one approved task. Null on the
    # project-level shares, which is how the ledger tells "your share of this
    # project" apart from "you were paid for this piece of it".
    task_title = serializers.CharField(source="task.title", read_only=True,
                                       default=None)

    class Meta:
        model = Earning
        fields = [
            "id", "project_id", "project_code", "project_title",
            "task", "task_title",
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
            "user_name", "user_role", "transfer_reference", "failure_reason",
        ]

    def get_user_name(self, obj):
        return obj.user.full_name or obj.user.email


class WithdrawalCreateSerializer(serializers.Serializer):
    """Only the amount — the destination comes from the saved payout account, so a
    request can never be pointed at an account Paystack hasn't verified."""

    amount_usd = serializers.DecimalField(max_digits=12, decimal_places=2)


class PayoutAccountSerializer(serializers.Serializer):
    """An earner's own bank account. Never exposed to anyone else."""

    bank_name = serializers.CharField(max_length=120)
    bank_code = serializers.CharField(max_length=20)
    bank_account_number = serializers.CharField(max_length=34)
    bank_account_name = serializers.CharField(max_length=150)

    def validate_bank_account_number(self, v):
        v = v.strip()
        if not v.isdigit():
            raise serializers.ValidationError("An account number should be digits only.")
        return v


class ResolveAccountSerializer(serializers.Serializer):
    bank_code = serializers.CharField(max_length=20)
    account_number = serializers.CharField(max_length=34)


class WithdrawalSettleSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=[
            Withdrawal.Status.PROCESSING,
            Withdrawal.Status.PAID,
            Withdrawal.Status.REJECTED,
        ]
    )
    note = serializers.CharField(required=False, allow_blank=True, default="")
    # True records a payout made outside Paystack instead of sending one.
    manual = serializers.BooleanField(required=False, default=False)


class TransferOtpSerializer(serializers.Serializer):
    otp = serializers.CharField(max_length=12)
