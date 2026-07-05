"""Seed the database with the prototype's personas and sample projects.

Idempotent: running it repeatedly won't create duplicates. All seeded accounts
share the password below so the demo is easy to sign into.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from projects.models import Activity, Project, Task

User = get_user_model()
DEMO_PASSWORD = "Ripple123!"


def upsert_user(email, **fields):
    user, created = User.objects.get_or_create(email=email, defaults=fields)
    if created:
        user.set_password(DEMO_PASSWORD)
        user.is_email_verified = True
        for k, v in fields.items():
            setattr(user, k, v)
        user.save()
    return user


DEVELOPERS = [
    ("chidi@ril.dev", "Chidi Okonkwo", "Full-stack · React · Node", 2),
    ("zainab@ril.dev", "Zainab Bello", "Frontend · UI", 1),
    ("emeka@ril.dev", "Emeka Nwosu", "Backend · Data", 3),
    ("ada@ril.dev", "Ada Eze", "Mobile · Flutter", 1),
    ("tunde@ril.dev", "Tunde Balogun", "Full-stack · DevOps", 2),
]


class Command(BaseCommand):
    help = "Seed demo users and sample projects."

    @transaction.atomic
    def handle(self, *args, **options):
        R = User.Role

        # --- Delivery lead (also a Django admin) ---
        lead = upsert_user(
            "ngozi@ril.team", full_name="Ngozi Adeyemi", role=R.DELIVERY_LEAD,
            company="Ripple Innovation Labs", is_staff=True, is_superuser=True,
        )

        # --- Developers ---
        devs = {}
        for email, name, specialty, load in DEVELOPERS:
            devs[name] = upsert_user(
                email, full_name=name, role=R.DEVELOPER,
                specialty=specialty, active_load=load,
            )

        # --- Clients ---
        amara = upsert_user("amara@hopebridge.org", full_name="Amara Okafor",
                            role=R.CLIENT, company="HopeBridge Foundation")
        lars = upsert_user("lars@nordicsoft.io", full_name="Lars Petersen",
                           role=R.CLIENT, company="NordicSoft")
        sofia = upsert_user("sofia@climadata.org", full_name="Sofia Romano",
                            role=R.CLIENT, company="ClimaData")
        daniel = upsert_user("daniel@bethshalom.org", full_name="Daniel Cohen",
                             role=R.CLIENT, company="Beth Shalom Community")
        grace = upsert_user("grace@agrireach.co", full_name="Grace Mwangi",
                            role=R.CLIENT, company="AgriReach")

        Stage = Project.Stage
        seed_projects = [
            {
                "code": "RIL-2041", "title": "Donor CRM Dashboard", "client": amara,
                "category": "Software development", "stage": Stage.IN_PROGRESS,
                "quote_usd": 4800, "developer": devs["Chidi Okonkwo"], "target_date": "Jul 18",
                "description": "A custom CRM to manage donors, track recurring gifts, and generate "
                               "board-ready reports. Should integrate with our existing mailing "
                               "list and export clean CSVs.",
                "tasks": [("Auth & role-based access", True), ("Contacts & segments module", True),
                          ("Donation timeline view", False), ("Reports & CSV export", False)],
                "activity": [
                    (amara, "Submitted the project brief."),
                    (lead, "Sent a quote of $4,800."),
                    (amara, "Paid the invoice via Paystack."),
                    (devs["Chidi Okonkwo"], "Kicked off the build — scaffolded the project, CI, and the database schema for donors and gifts.", Activity.Kind.PROGRESS),
                    (devs["Chidi Okonkwo"], "Shipped auth with role-based access and the contacts & segments module. Both are on staging for you to try.", Activity.Kind.MILESTONE),
                    (devs["Chidi Okonkwo"], "Starting the donation timeline view next. One question: should recurring gifts show as a single series or individual entries?", Activity.Kind.QUESTION),
                ],
            },
            {
                "code": "RIL-2038", "title": "Grant Reporting Tool", "client": amara,
                "category": "Software development", "stage": Stage.QUOTED, "quote_usd": 2600,
                "developer": None, "target_date": "Jul 24",
                "description": "A lightweight tool to compile quarterly grant reports from our "
                               "program data and export a formatted PDF for funders.",
                "tasks": [],
                "activity": [
                    (amara, "Submitted the project brief."),
                    (lead, "Sent a quote of $2,600 — ready for payment."),
                ],
            },
            {
                "code": "RIL-2044", "title": "Volunteer Mobile App", "client": amara,
                "category": "Software development", "stage": Stage.SUBMITTED, "quote_usd": 0,
                "developer": None, "target_date": "TBD",
                "description": "A simple mobile app for volunteers to view shifts, check in, and "
                               "log hours. iOS and Android.",
                "tasks": [],
                "activity": [(amara, "Submitted the project brief. Awaiting a quote.")],
            },
            {
                "code": "RIL-2029", "title": "Marketing Website Rebuild", "client": lars,
                "category": "Website development", "stage": Stage.REVIEW, "quote_usd": 3200,
                "developer": devs["Zainab Bello"], "target_date": "Jul 5",
                "description": "Rebuild our marketing site with a modern CMS, faster load times, "
                               "and a refreshed brand look.",
                "tasks": [("Design system & pages", True), ("CMS integration", True),
                          ("Responsive & performance pass", True)],
                "activity": [
                    (devs["Zainab Bello"], "Rebuilt all pages on the new design system and wired up the CMS.", Activity.Kind.PROGRESS),
                    (devs["Zainab Bello"], "Ran a performance pass — Lighthouse is now 98/100 on mobile.", Activity.Kind.PROGRESS),
                    (devs["Zainab Bello"], "Submitted the site for client review — ready for your sign-off.", Activity.Kind.MILESTONE),
                ],
            },
            {
                "code": "RIL-2035", "title": "Climate Data Pipeline", "client": sofia,
                "category": "Data analysis", "stage": Stage.PAID, "quote_usd": 5600,
                "developer": None, "target_date": "Jul 30",
                "description": "An automated pipeline to ingest sensor data, clean it, and surface "
                               "a live dashboard for our research team.",
                "tasks": [],
                "activity": [(sofia, "Paid the invoice via Paystack. Ready to start.")],
            },
            {
                "code": "RIL-2050", "title": "API Integration Layer", "client": daniel,
                "category": "Software development", "stage": Stage.IN_PROGRESS, "quote_usd": 3900,
                "developer": devs["Chidi Okonkwo"], "target_date": "Jul 15",
                "description": "A middleware layer connecting our membership system to payments and "
                               "email, with a Paystack payment sync.",
                "tasks": [("Webhook receiver", True), ("Payment sync (Paystack)", False),
                          ("Retry & error queue", False)],
                "activity": [
                    (devs["Chidi Okonkwo"], "Finished the webhook receiver and verified signatures against Paystack test events.", Activity.Kind.PROGRESS),
                    (devs["Chidi Okonkwo"], "Heads up: the membership API rate-limits us at 60 req/min, so the payment sync needs a queue. Building that now — may add half a day.", Activity.Kind.BLOCKER),
                ],
            },
            {
                "code": "RIL-2018", "title": "WhatsApp Automation Bot", "client": grace,
                "category": "AI integration & automation", "stage": Stage.COMPLETED, "quote_usd": 2100,
                "developer": devs["Emeka Nwosu"], "target_date": "Jun 20",
                "description": "A WhatsApp bot that answers farmer FAQs and routes complex questions "
                               "to an agronomist.",
                "tasks": [("Bot flows & intents", True), ("Agronomist handoff", True)],
                "activity": [(grace, "Approved delivery. Project complete.")],
            },
        ]

        created_count = 0
        for spec in seed_projects:
            if Project.objects.filter(code=spec["code"]).exists():
                continue
            project = Project.objects.create(
                code=spec["code"], title=spec["title"], client=spec["client"],
                company=spec["client"].company, category=spec["category"],
                stage=spec["stage"], quote_usd=spec["quote_usd"],
                developer=spec["developer"], target_date=spec["target_date"],
                description=spec["description"],
            )
            for i, (title, done) in enumerate(spec["tasks"]):
                Task.objects.create(project=project, title=title, done=done,
                                    assignee=spec["developer"], order=i)
            for entry in spec["activity"]:
                author, text = entry[0], entry[1]
                kind = entry[2] if len(entry) > 2 else Activity.Kind.SYSTEM
                Activity.objects.create(project=project, author=author,
                                        author_name=author.full_name,
                                        role_label=author.role_label,
                                        kind=kind, text=text)
            created_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"Seed complete. {created_count} new projects. "
            f"All demo accounts use password: {DEMO_PASSWORD}"
        ))
        self.stdout.write("Delivery Lead: ngozi@ril.team")
        self.stdout.write("Client:        amara@hopebridge.org")
        self.stdout.write("Developer:     chidi@ril.dev")
