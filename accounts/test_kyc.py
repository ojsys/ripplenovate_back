"""Profile detail, CV upload, and identity verification.

The tests that matter here are access boundaries. A CV and a passport scan have
different audiences: a delivery lead needs the first to staff a brief and has no
business seeing the second. Getting that wrong leaks documents, so it is pinned
from several angles.
"""
import io

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import KycProfile, ProfessionalProfile, SiteSettings
from catalog.models import ProductLine

User = get_user_model()


def a_file(name="cv.pdf", size=1024):
    return SimpleUploadedFile(name, b"x" * size, content_type="application/pdf")


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class ProfileTestCase(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "kyclead@ril.team", "x", full_name="Their Lead",
            role=User.Role.DELIVERY_LEAD)
        self.other_lead = User.objects.create_user(
            "otherlead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.expert = User.objects.create_user(
            "kycexpert@ril.dev", "x", full_name="An Expert",
            role=User.Role.EXPERT, lead=self.lead)
        self.admin = User.objects.create_superuser("kycadmin@ril.team", "x")
        self.customer = User.objects.create_user(
            "kycclient@acme.io", "x", role=User.Role.CLIENT)


class ProfessionalProfileTests(ProfileTestCase):
    def test_an_expert_fills_in_their_professional_detail(self):
        response = as_user(self.expert).patch("/api/profile/professional", {
            "bio": "Ten years in brand design.",
            "languages": ["English", "Yoruba"],
            "availability_hours": 30,
            "certifications": "Google UX Certificate",
        }, format="json")
        self.assertEqual(response.status_code, 200)
        profile = ProfessionalProfile.objects.get(user=self.expert)
        self.assertEqual(profile.languages, ["English", "Yoruba"])
        self.assertEqual(profile.availability_hours, 30)

    def test_availability_beyond_a_week_is_rejected(self):
        response = as_user(self.expert).patch(
            "/api/profile/professional", {"availability_hours": 200}, format="json")
        self.assertEqual(response.status_code, 400)


@override_settings(MEDIA_ROOT="/tmp/ril-test-media")
class CvUploadTests(ProfileTestCase):
    def upload(self, user=None, **kw):
        return as_user(user or self.expert).post(
            "/api/profile/cv", {"cv": a_file(**kw)}, format="multipart")

    def test_an_expert_uploads_a_cv(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["has_cv"])
        profile = ProfessionalProfile.objects.get(user=self.expert)
        self.assertTrue(profile.cv)
        self.assertIsNotNone(profile.cv_uploaded_at)

    def test_the_stored_filename_does_not_reveal_who_it_belongs_to(self):
        self.upload(name="An-Expert-Resume-2026.pdf")
        stored = ProfessionalProfile.objects.get(user=self.expert).cv.name
        self.assertNotIn("An-Expert", stored)
        self.assertTrue(stored.startswith("cv/"))
        self.assertTrue(stored.endswith(".pdf"))

    def test_an_executable_is_refused(self):
        response = self.upload(name="payload.exe")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ProfessionalProfile.objects.filter(
            user=self.expert).exclude(cv="").exists())

    def test_an_oversized_file_is_refused(self):
        response = self.upload(size=6 * 1024 * 1024)
        self.assertEqual(response.status_code, 400)

    def test_replacing_a_cv_does_not_leave_the_old_one_behind(self):
        self.upload()
        first = ProfessionalProfile.objects.get(user=self.expert).cv.path
        self.upload()
        second = ProfessionalProfile.objects.get(user=self.expert).cv.path
        self.assertNotEqual(first, second)
        import os
        self.assertFalse(os.path.exists(first))

    def test_their_own_lead_can_download_the_cv(self):
        self.upload()
        response = as_user(self.lead).get(f"/api/documents/cv/{self.expert.id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        # A personal document must not be cached by a proxy or the browser.
        self.assertIn("no-store", response["Cache-Control"])

    def test_another_lead_cannot_download_the_cv(self):
        """Only the lead this expert actually works under — not any lead."""
        self.upload()
        response = as_user(self.other_lead).get(f"/api/documents/cv/{self.expert.id}")
        self.assertEqual(response.status_code, 403)

    def test_a_client_cannot_download_a_cv(self):
        self.upload()
        response = as_user(self.customer).get(f"/api/documents/cv/{self.expert.id}")
        self.assertEqual(response.status_code, 403)

    def test_an_expert_can_remove_their_cv(self):
        self.upload()
        response = as_user(self.expert).delete("/api/profile/cv")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(ProfessionalProfile.objects.get(user=self.expert).cv)


@override_settings(MEDIA_ROOT="/tmp/ril-test-media")
class KycTests(ProfileTestCase):
    COMPLETE = {
        "legal_name": "Adaeze N. Expert",
        "date_of_birth": "1994-03-11",
        "phone": "+234 800 000 0000",
        "address_line1": "12 Marina Road",
        "city": "Lagos",
        "country": "Nigeria",
        "id_type": "passport",
        "id_number": "A01234567",
    }

    def fill(self, user=None, **overrides):
        client = as_user(user or self.expert)
        client.patch("/api/profile/kyc", {**self.COMPLETE, **overrides}, format="json")
        client.post("/api/profile/kyc/document",
                    {"document": a_file("passport.jpg")}, format="multipart")
        return client

    def test_the_id_number_is_never_echoed_back(self):
        """Not even to its owner — they know it, and not echoing it means a
        cached response or a shoulder-surf never exposes it."""
        client = as_user(self.expert)
        client.patch("/api/profile/kyc", self.COMPLETE, format="json")
        data = client.get("/api/profile/kyc").data
        self.assertNotIn("id_number", data)
        self.assertEqual(data["id_number_masked"], "•••• 4567")

    def test_submission_lists_what_is_still_missing(self):
        client = as_user(self.expert)
        client.patch("/api/profile/kyc", {"legal_name": "Adaeze"}, format="json")
        response = client.post("/api/profile/kyc/submit")
        self.assertEqual(response.status_code, 400)
        self.assertIn("date_of_birth", response.data["missing"])
        self.assertIn("id_document", response.data["missing"])

    def test_a_complete_record_can_be_submitted(self):
        response = self.fill().post("/api/profile/kyc/submit")
        self.assertEqual(response.status_code, 200)
        kyc = KycProfile.objects.get(user=self.expert)
        self.assertEqual(kyc.status, KycProfile.Status.PENDING)
        self.assertIsNotNone(kyc.submitted_at)

    def test_an_admin_verifies_it(self):
        self.fill().post("/api/profile/kyc/submit")
        response = as_user(self.admin).post(
            f"/api/verifications/{self.expert.id}/decide", {"decision": "verify"})
        self.assertEqual(response.status_code, 200)
        kyc = KycProfile.objects.get(user=self.expert)
        self.assertTrue(kyc.is_verified)
        self.assertEqual(kyc.reviewed_by, self.admin)

    def test_a_rejection_records_a_reason_for_the_person(self):
        self.fill().post("/api/profile/kyc/submit")
        as_user(self.admin).post(
            f"/api/verifications/{self.expert.id}/decide",
            {"decision": "reject", "reason": "The photo is too blurred to read."})
        kyc = KycProfile.objects.get(user=self.expert)
        self.assertEqual(kyc.status, KycProfile.Status.REJECTED)
        self.assertEqual(kyc.rejection_reason, "The photo is too blurred to read.")

    def test_a_verified_record_is_frozen(self):
        """Otherwise someone could swap their identity after being cleared."""
        self.fill().post("/api/profile/kyc/submit")
        KycProfile.objects.get(user=self.expert).verify()
        response = as_user(self.expert).patch(
            "/api/profile/kyc", {"legal_name": "Someone Else"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            KycProfile.objects.get(user=self.expert).legal_name, "Adaeze N. Expert")

    def test_a_delivery_lead_cannot_read_the_review_queue(self):
        """Identity documents are for admins. A lead staffs work; that needs a
        CV, not a date of birth."""
        self.fill().post("/api/profile/kyc/submit")
        self.assertEqual(as_user(self.lead).get("/api/verifications").status_code, 403)
        self.assertEqual(as_user(self.admin).get("/api/verifications").status_code, 200)

    def test_a_delivery_lead_cannot_download_an_id_document(self):
        self.fill()
        self.assertEqual(
            as_user(self.lead).get(f"/api/documents/id/{self.expert.id}").status_code, 403)
        self.assertEqual(
            as_user(self.admin).get(f"/api/documents/id/{self.expert.id}").status_code, 200)
        self.assertEqual(
            as_user(self.expert).get(f"/api/documents/id/{self.expert.id}").status_code, 200)

    def test_a_lead_cannot_decide_a_verification(self):
        self.fill().post("/api/profile/kyc/submit")
        response = as_user(self.lead).post(
            f"/api/verifications/{self.expert.id}/decide", {"decision": "verify"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(KycProfile.objects.get(user=self.expert).is_verified)

    def test_a_client_has_no_identity_record(self):
        self.assertEqual(as_user(self.customer).get("/api/profile/kyc").status_code, 403)


@override_settings(MEDIA_ROOT="/tmp/ril-test-media")
class KycPayoutGateTests(ProfileTestCase):
    def setUp(self):
        super().setUp()
        self.expert.bank_name = "Test Bank"
        self.expert.bank_code = "058"
        self.expert.bank_account_number = "0123456789"
        self.expert.bank_account_name = "An Expert"
        self.expert.save()

    def request_payout(self):
        from payments import earnings as service
        return service.request_withdrawal(self.expert, 50)

    def test_the_gate_is_off_by_default(self):
        """Turning KYC on must never silently strand people already owed money."""
        from payments.earnings import WithdrawalError

        self.assertFalse(SiteSettings.payout_config()["require_kyc_for_payout"])
        with self.assertRaises(WithdrawalError) as ctx:
            self.request_payout()
        # Refused for lack of balance, not for lack of verification.
        self.assertIn("withdraw up to", str(ctx.exception))

    def test_when_switched_on_an_unverified_earner_is_blocked(self):
        from payments.earnings import WithdrawalError

        row = SiteSettings.load()
        row.require_kyc_for_payout = True
        row.save()

        with self.assertRaises(WithdrawalError) as ctx:
            self.request_payout()
        self.assertIn("verified", str(ctx.exception))
