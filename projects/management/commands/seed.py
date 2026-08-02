"""Seed the database with the prototype's personas and sample projects.

Idempotent: running it repeatedly won't create duplicates. All seeded accounts
share the password below so the demo is easy to sign into.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from catalog.models import ProductLine, Service
from catalog.seed_data import LEGACY_CATEGORY_MAP, PRODUCT_LINES
from projects.models import Activity, Attachment, Project, Task

User = get_user_model()
DEMO_PASSWORD = "Ripple123!"


def upsert_user(email, **fields):
    """Create or refresh a demo account.

    Profile fields are re-applied on every run, not just at creation — otherwise
    a persona that gained a field (a product line, a roster lead) would keep the
    stale version forever on any database that was seeded before. The password
    is only ever set on creation, so re-seeding never clobbers a changed one.
    """
    user, created = User.objects.get_or_create(email=email, defaults=fields)
    if created:
        user.set_password(DEMO_PASSWORD)
        user.is_email_verified = True
    for key, value in fields.items():
        setattr(user, key, value)
    user.save()
    return user


# Deliberately spread across disciplines, not just software — the platform
# delivers design, data and research work too.
EXPERTS = [
    # (email, name, specialty, active load, product line slugs, skills)
    ("chidi@ril.dev", "Chidi Okonkwo", "Full-stack · React · Node", 2,
     ["software-web"], ["React", "Node", "PostgreSQL"]),
    ("zainab@ril.dev", "Zainab Bello", "Product design · UI/UX", 1,
     ["design-creative"], ["Figma", "Prototyping", "Design systems"]),
    ("emeka@ril.dev", "Emeka Nwosu", "Backend · Data engineering", 3,
     ["software-web", "data-research"], ["Python", "Django", "dbt"]),
    ("ada@ril.dev", "Ada Eze", "Brand & graphic design", 1,
     ["design-creative"], ["Illustrator", "Brand identity", "Motion"]),
    ("tunde@ril.dev", "Tunde Balogun", "Full-stack · DevOps", 2,
     ["software-web"], ["Go", "Kubernetes", "CI/CD"]),
    ("halima@ril.dev", "Halima Sani", "Research & data analysis", 1,
     ["data-research"], ["Stata", "Power BI", "Survey design"]),
]

# The lead runs every line for the demo, so one sign-in shows the whole board.
LEAD_LINES = [line[0] for line in PRODUCT_LINES]


# (project code, purpose, url, label)
SEED_LINKS = [
    ("RIL-2029", "deliverable", "https://figma.com/file/nordicsoft-site", "Final site designs"),
    ("RIL-2029", "reference", "https://drive.google.com/drive/folders/brand", "NordicSoft brand assets"),
    ("RIL-2062", "reference", "https://drive.google.com/drive/folders/bethshalom", "Photos & old logo"),
    ("RIL-2062", "deliverable", "https://figma.com/file/bethshalom-marks", "Logo directions v2"),
    ("RIL-2035", "deliverable", "https://docs.google.com/spreadsheets/d/climadata", "Analysis & findings"),
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
        lines = {line.slug: line for line in ProductLine.objects.all()}
        lead.product_lines.set([lines[s] for s in LEAD_LINES if s in lines])

        # --- Experts, spread across the disciplines the platform delivers ---
        experts = {}
        for email, name, specialty, load, slugs, skills in EXPERTS:
            expert = upsert_user(
                email, full_name=name, role=R.EXPERT,
                specialty=specialty, active_load=load, skills=skills,
                lead=lead,
            )
            expert.product_lines.set([lines[s] for s in slugs if s in lines])
            experts[name] = expert

        # --- Business developer ---
        bizdev = upsert_user(
            "kofi@ril.team", full_name="Kofi Mensah", role=R.BUSINESS_DEV,
            company="Ripple Innovation Labs",
        )
        bizdev.ensure_referral_code()

        # --- Clients ---
        amara = upsert_user("amara@hopebridge.org", full_name="Amara Okafor",
                            role=R.CLIENT, company="HopeBridge Foundation")
        lars = upsert_user("lars@nordicsoft.io", full_name="Lars Petersen",
                           role=R.CLIENT, company="NordicSoft")
        sofia = upsert_user("sofia@climadata.org", full_name="Sofia Romano",
                            role=R.CLIENT, company="ClimaData")
        daniel = upsert_user("daniel@bethshalom.org", full_name="Daniel Cohen",
                             role=R.CLIENT, company="Beth Shalom Community",
                             referred_by=bizdev)
        grace = upsert_user("grace@agrireach.co", full_name="Grace Mwangi",
                            role=R.CLIENT, company="AgriReach",
                            referred_by=bizdev)

        Stage = Project.Stage
        seed_projects = [
            {
                "code": "RIL-2041", "title": "Donor CRM Dashboard", "client": amara,
                "category": "Software development", "stage": Stage.IN_PROGRESS,
                "quote_usd": 4800, "expert": experts["Chidi Okonkwo"], "target_days": -6,
                "description": "A custom CRM to manage donors, track recurring gifts, and generate "
                               "board-ready reports. Should integrate with our existing mailing "
                               "list and export clean CSVs.",
                "tasks": [("Auth & role-based access", True), ("Contacts & segments module", True),
                          ("Donation timeline view", False), ("Reports & CSV export", False)],
                "activity": [
                    (amara, "Submitted the project brief."),
                    (lead, "Sent a quote of $4,800."),
                    (amara, "Paid the invoice via Paystack."),
                    (experts["Chidi Okonkwo"], "Kicked off the build — scaffolded the project, CI, and the database schema for donors and gifts.", Activity.Kind.PROGRESS),
                    (experts["Chidi Okonkwo"], "Shipped auth with role-based access and the contacts & segments module. Both are on staging for you to try.", Activity.Kind.MILESTONE),
                    (experts["Chidi Okonkwo"], "Starting the donation timeline view next. One question: should recurring gifts show as a single series or individual entries?", Activity.Kind.QUESTION),
                ],
            },
            {
                "code": "RIL-2038", "title": "Grant Reporting Tool", "client": amara,
                "category": "Software development", "stage": Stage.QUOTED, "quote_usd": 2600,
                "expert": None, "target_days": 30,
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
                "expert": None, "target_days": None,
                "description": "A simple mobile app for volunteers to view shifts, check in, and "
                               "log hours. iOS and Android.",
                "tasks": [],
                "activity": [(amara, "Submitted the project brief. Awaiting a quote.")],
            },
            {
                "code": "RIL-2029", "title": "Marketing Website Rebuild", "client": lars,
                "category": "Website development", "stage": Stage.REVIEW, "quote_usd": 3200,
                "expert": experts["Zainab Bello"], "target_days": -4,
                "description": "Rebuild our marketing site with a modern CMS, faster load times, "
                               "and a refreshed brand look.",
                "tasks": [("Design system & pages", True), ("CMS integration", True),
                          ("Responsive & performance pass", True)],
                "activity": [
                    (experts["Zainab Bello"], "Rebuilt all pages on the new design system and wired up the CMS.", Activity.Kind.PROGRESS),
                    (experts["Zainab Bello"], "Ran a performance pass — Lighthouse is now 98/100 on mobile.", Activity.Kind.PROGRESS),
                    (experts["Zainab Bello"], "Submitted the site for client review — ready for your sign-off.", Activity.Kind.MILESTONE),
                ],
            },
            {
                "code": "RIL-2035", "title": "Climate Data Pipeline", "client": sofia,
                "category": "Data analysis", "stage": Stage.PAID, "quote_usd": 5600,
                "expert": None, "target_days": 40,
                "description": "An automated pipeline to ingest sensor data, clean it, and surface "
                               "a live dashboard for our research team.",
                "tasks": [],
                "activity": [(sofia, "Paid the invoice via Paystack. Ready to start.")],
            },
            {
                "code": "RIL-2050", "title": "API Integration Layer", "client": daniel,
                "category": "Software development", "stage": Stage.IN_PROGRESS, "quote_usd": 3900,
                "expert": experts["Chidi Okonkwo"], "target_days": 18,
                "description": "A middleware layer connecting our membership system to payments and "
                               "email, with a Paystack payment sync.",
                "tasks": [("Webhook receiver", True), ("Payment sync (Paystack)", False),
                          ("Retry & error queue", False)],
                "activity": [
                    (experts["Chidi Okonkwo"], "Finished the webhook receiver and verified signatures against Paystack test events.", Activity.Kind.PROGRESS),
                    (experts["Chidi Okonkwo"], "Heads up: the membership API rate-limits us at 60 req/min, so the payment sync needs a queue. Building that now — may add half a day.", Activity.Kind.BLOCKER),
                ],
            },
            {
                "code": "RIL-2018", "title": "WhatsApp Automation Bot", "client": grace,
                "category": "AI integration & automation", "stage": Stage.COMPLETED, "quote_usd": 2100,
                "expert": experts["Emeka Nwosu"], "target_days": 9,
                # Delivered three days ahead of the promised date.
                "completed_days": -3,
                "description": "A WhatsApp bot that answers farmer FAQs and routes complex questions "
                               "to an agronomist.",
                "tasks": [("Bot flows & intents", True), ("Agronomist handoff", True)],
                "activity": [(grace, "Approved delivery. Project complete.")],
            },
            {
                "code": "RIL-2062", "title": "Brand Identity & Style Guide", "client": daniel,
                "category": "Brand identity", "stage": Stage.IN_PROGRESS, "quote_usd": 2400,
                "expert": experts["Ada Eze"], "target_days": 14,
                "description": "A full visual identity for our community centre — logo, colour "
                               "palette, typography, and a usage guide our volunteers can follow.",
                "tasks": [("Discovery & moodboard", True), ("Logo concepts", True),
                          ("Palette & type system", False), ("Usage guide", False)],
                "activity": [
                    (daniel, "Submitted the project brief."),
                    (lead, "Sent a quote of $2,400."),
                    (experts["Ada Eze"], "Three logo directions ready for review — sharing the "
                                         "board now. Direction 2 tested best with the volunteers.",
                     Activity.Kind.MILESTONE),
                ],
            },
            {
                "code": "RIL-2068", "title": "Smallholder Market Study", "client": grace,
                "category": "Market research", "stage": Stage.SUBMITTED, "quote_usd": 0,
                "expert": None, "target_days": None,
                "description": "Market sizing and pricing research across three states, with "
                               "farmer interviews and a written findings report.",
                "tasks": [],
                "activity": [(grace, "Submitted the project brief. Awaiting a quote.")],
            },
        ]

        def line_and_service(category):
            """Resolve a seeded category to its product line and service.

            Handles both the legacy free-text categories and the new service
            names, so the seed keeps working either way.
            """
            service = Service.objects.filter(name=category).first()
            if service:
                return service.product_line, service
            slug, service_name = LEGACY_CATEGORY_MAP.get(category, (None, None))
            line = lines.get(slug)
            if not line:
                return None, None
            return line, Service.objects.filter(
                product_line=line, name=service_name).first()

        def line_for(category):
            return line_and_service(category)[0]

        def service_for(category):
            return line_and_service(category)[1]

        created_count = 0
        for spec in seed_projects:
            if Project.objects.filter(code=spec["code"]).exists():
                continue
            project = Project.objects.create(
                code=spec["code"], title=spec["title"], client=spec["client"],
                company=spec["client"].company, category=spec["category"],
                product_line=line_for(spec["category"]),
                service=service_for(spec["category"]),
                stage=spec["stage"], quote_usd=spec["quote_usd"],
                business_developer=spec["client"].referred_by,
                expert=spec["expert"],
                target_date=(
                    timezone.localdate() + timedelta(days=spec["target_days"])
                    if spec["target_days"] is not None else None
                ),
                completed_at=(
                    timezone.now() + timedelta(days=spec["completed_days"])
                    if spec.get("completed_days") is not None else None
                ),
                description=spec["description"],
                # Quoted briefs and beyond are owned by the lead who quoted them.
                lead=lead if spec["stage"] != Stage.SUBMITTED else None,
            )
            for i, (title, done) in enumerate(spec["tasks"]):
                Task.objects.create(project=project, title=title, done=done,
                                    assignee=spec["expert"], order=i)
            for entry in spec["activity"]:
                author, text = entry[0], entry[1]
                kind = entry[2] if len(entry) > 2 else Activity.Kind.SYSTEM
                Activity.objects.create(project=project, author=author,
                                        author_name=author.full_name,
                                        role_label=author.role_label,
                                        kind=kind, text=text)
            created_count += 1

        # Credit the earnings for anything seeded as already delivered, so the
        # ledger is right from the first run rather than after someone opens a
        # page that happens to trigger the backfill.
        from payments import earnings as earnings_service

        for project in Project.objects.filter(stage=Stage.COMPLETED):
            earnings_service.record_project_earnings(project)

        # Deliverable and reference links on a few projects.
        for code, purpose, url, label in SEED_LINKS:
            project = Project.objects.filter(code=code).first()
            if not project or project.attachments.filter(url=url).exists():
                continue
            Attachment.objects.create(
                project=project, url=url, label=label, purpose=purpose,
                added_by=project.expert if purpose == "deliverable" else project.client,
            )

        self.stdout.write(self.style.SUCCESS(
            f"Seed complete. {created_count} new projects. "
            f"All demo accounts use password: {DEMO_PASSWORD}"
        ))
        self.stdout.write("Delivery Lead: ngozi@ril.team")
        self.stdout.write("Client:        amara@hopebridge.org")
        self.stdout.write("Expert:        chidi@ril.dev")
        self.stdout.write("Business Dev:  kofi@ril.team")
