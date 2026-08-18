"""Replies and deliverable-anchored comments (G6).

The activity feed was a good *record* and a poor *conversation*. Typed updates
told you what state the work was in; they gave a client with a question about
one Figma frame nowhere to put it. Most bounced work is a misunderstanding about
one specific file, so anchoring matters at least as much as threading.

Two rules carry the design, and both are tested here: the feed is exactly two
levels deep whatever the client sends, and a reply notifies the conversation
rather than the whole project.
"""
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from rest_framework.test import APIClient

from catalog.models import ProductLine
from projects.models import Activity, Attachment, Project

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class ThreadTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "thlead@ril.team", "x", full_name="A Lead",
            role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.ada = User.objects.create_user(
            "thada@ril.dev", "x", full_name="Ada Eze",
            role=User.Role.EXPERT, lead=self.lead)
        self.chidi = User.objects.create_user(
            "thchidi@ril.dev", "x", full_name="Chidi Okonkwo",
            role=User.Role.EXPERT, lead=self.lead)
        self.customer = User.objects.create_user(
            "thclient@acme.io", "x", full_name="A Buyer", role=User.Role.CLIENT)
        self.stranger = User.objects.create_user(
            "thnosy@acme.io", "x", role=User.Role.CLIENT)

        self.project = Project.objects.create(
            title="A rebrand", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.ada, stage=Project.Stage.IN_PROGRESS, quote_usd=5000)
        self.project.experts.add(self.ada, self.chidi)
        self.deck = Attachment.objects.create(
            project=self.project, url="https://figma.com/file/abc",
            label="Brand deck", purpose=Attachment.Purpose.DELIVERABLE,
            added_by=self.ada)

    def url(self, suffix=""):
        return f"/api/projects/{self.project.id}{suffix}"

    def post(self, by=None, **payload):
        payload.setdefault("text", "A comment")
        return as_user(by or self.ada).post(
            self.url("/activity"), payload, format="json")

    def root(self, by=None, text="The first draft is up", kind="progress"):
        self.post(by=by or self.ada, text=text, kind=kind)
        return Activity.objects.filter(parent__isnull=True).latest("id")

    # --- replying ---
    def test_a_reply_attaches_to_its_parent(self):
        parent = self.root()
        response = self.post(by=self.customer, text="Looks good.", parent=parent.id)
        self.assertEqual(response.status_code, 200, response.data)
        reply = Activity.objects.latest("id")
        self.assertEqual(reply.parent_id, parent.id)

    def test_replies_are_never_typed(self):
        """A reply agreeing with a Blocker must not add a second Blocker chip."""
        parent = self.root(kind="blocker")
        self.post(by=self.customer, text="Agreed.", parent=parent.id, kind="blocker")
        self.assertEqual(Activity.objects.latest("id").kind, Activity.Kind.UPDATE)

    def test_the_feed_never_goes_three_deep(self):
        """Replying to a reply lands beside it, not under it."""
        parent = self.root()
        self.post(by=self.customer, text="First reply", parent=parent.id)
        first = Activity.objects.latest("id")
        self.post(by=self.ada, text="Reply to the reply", parent=first.id)
        second = Activity.objects.latest("id")
        self.assertEqual(second.parent_id, parent.id,
                         "should flatten to the top-level entry")
        self.assertEqual(first.replies.count(), 0)

    def test_you_cannot_reply_across_projects(self):
        other = Project.objects.create(
            title="Elsewhere", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            stage=Project.Stage.IN_PROGRESS, quote_usd=1000)
        foreign = Activity.objects.create(
            project=other, author=self.lead, author_name="A Lead",
            role_label="Delivery Lead", text="Over here")
        response = self.post(text="Sneaking in", parent=foreign.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("isn't on this project", str(response.data))

    def test_a_reply_inherits_the_projects_access_rules(self):
        parent = self.root()
        self.assertIn(
            as_user(self.stranger).post(
                self.url("/activity"),
                {"text": "Not mine", "parent": parent.id}, format="json").status_code,
            (403, 404))

    # --- anchoring to a deliverable ---
    def test_a_comment_can_be_about_one_file(self):
        response = self.post(by=self.customer, text="Slide 2 is wrong.",
                             attachment=self.deck.id)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Activity.objects.latest("id").attachment_id, self.deck.id)

    def test_you_cannot_anchor_to_another_projects_file(self):
        other = Project.objects.create(
            title="Elsewhere", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            stage=Project.Stage.IN_PROGRESS, quote_usd=1000)
        foreign = Attachment.objects.create(
            project=other, url="https://figma.com/file/zzz", label="Theirs",
            purpose=Attachment.Purpose.DELIVERABLE, added_by=self.lead)
        response = self.post(text="About theirs", attachment=foreign.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("isn't on this project", str(response.data))

    def test_an_unanchored_update_still_works(self):
        response = self.post(text="General progress", kind="progress")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(Activity.objects.latest("id").attachment_id)

    # --- how it reads back ---
    def test_replies_come_nested_and_not_also_flat(self):
        parent = self.root()
        self.post(by=self.customer, text="One", parent=parent.id)
        self.post(by=self.chidi, text="Two", parent=parent.id)

        feed = as_user(self.lead).get(self.url()).data["activity"]
        roots = [a for a in feed if a["id"] == parent.id]
        self.assertEqual(len(roots), 1)
        self.assertEqual(len(roots[0]["replies"]), 2)
        texts = {a["text"] for a in feed}
        self.assertNotIn("One", texts, "a reply appeared as a top-level entry")

    def test_replies_read_oldest_first(self):
        parent = self.root()
        self.post(by=self.customer, text="First", parent=parent.id)
        self.post(by=self.chidi, text="Second", parent=parent.id)
        feed = as_user(self.lead).get(self.url()).data["activity"]
        replies = next(a for a in feed if a["id"] == parent.id)["replies"]
        self.assertEqual([r["text"] for r in replies], ["First", "Second"])

    def test_the_anchor_is_exposed_so_the_ui_can_group(self):
        self.post(by=self.customer, text="About the deck", attachment=self.deck.id)
        feed = as_user(self.lead).get(self.url()).data["activity"]
        anchored = next(a for a in feed if a["text"] == "About the deck")
        self.assertEqual(anchored["attachment"], self.deck.id)

    # --- who hears about it ---
    def test_a_top_level_update_still_reaches_everyone(self):
        mail.outbox = []
        self.root(by=self.ada)
        recipients = {addr for m in mail.outbox for addr in m.to}
        self.assertIn(self.customer.email, recipients)
        self.assertIn(self.chidi.email, recipients)
        self.assertIn(self.lead.email, recipients)
        self.assertNotIn(self.ada.email, recipients, "the author was emailed")

    def test_a_reply_reaches_the_thread_not_the_whole_team(self):
        """Two people going back and forth about one file must not mail six."""
        parent = self.root(by=self.ada)
        mail.outbox = []
        self.post(by=self.customer, text="A question", parent=parent.id)

        recipients = {addr for m in mail.outbox for addr in m.to}
        self.assertIn(self.ada.email, recipients, "the person replied to")
        self.assertIn(self.lead.email, recipients, "the accountable lead")
        self.assertNotIn(self.chidi.email, recipients,
                         "an uninvolved teammate was pulled in")
        self.assertNotIn(self.customer.email, recipients, "the author was emailed")

    def test_answering_a_thread_puts_you_in_it(self):
        parent = self.root(by=self.ada)
        self.post(by=self.chidi, text="I'll take this", parent=parent.id)
        mail.outbox = []
        self.post(by=self.customer, text="Thanks", parent=parent.id)

        recipients = {addr for m in mail.outbox for addr in m.to}
        self.assertIn(self.chidi.email, recipients,
                      "someone who joined the thread should stay in it")
        self.assertIn(self.ada.email, recipients)

    def test_the_notification_says_what_the_comment_is_about(self):
        mail.outbox = []
        self.post(by=self.customer, text="Slide 2 is wrong.",
                  attachment=self.deck.id)
        body = " ".join(m.body for m in mail.outbox)
        self.assertIn("Brand deck", body)

    def test_a_reply_quotes_what_it_answers(self):
        parent = self.root(by=self.ada, text="Which logo direction do you prefer?")
        mail.outbox = []
        self.post(by=self.customer, text="The second one.", parent=parent.id)
        body = " ".join(m.body for m in mail.outbox)
        self.assertIn("Which logo direction", body)

    # --- the bell agrees with the mail ---
    def test_the_two_channels_still_match_on_a_reply(self):
        from accounts.models import Notification

        parent = self.root(by=self.ada)
        mail.outbox = []
        Notification.objects.all().delete()
        self.post(by=self.customer, text="A question", parent=parent.id)

        emailed = {addr for m in mail.outbox for addr in m.to}
        belled = set(Notification.objects.values_list("user__email", flat=True))
        self.assertEqual(emailed, belled)
