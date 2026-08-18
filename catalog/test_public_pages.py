"""Indexable service pages (G11).

The platform had no organic acquisition surface at all — no directory, nothing
for a search engine to find — so every client had to be hand-carried in by a
business developer and growth was entirely gated on BD headcount.

These pages fix that **without becoming a marketplace**. Most of the tests here
are about that restraint: they check what the pages don't say as carefully as
what they do. A public talent directory is the surface this platform
deliberately doesn't have, and a statistic drawn from three projects is a fact
about three clients rather than about the platform.
"""
import json
import re
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from catalog import public
from catalog.models import ProductLine, Service
from projects.models import Project

User = get_user_model()


class PageBuildTests(TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.line = ProductLine.objects.get(slug="design-creative")

    def build(self, **kwargs):
        call_command("build_service_pages", out=str(self.out),
                     site="https://example.test", clean=True, **kwargs)

    def read(self, *parts):
        path = self.out.joinpath(*parts)
        return path.read_text() if path.exists() else None

    def test_every_active_line_gets_a_page(self):
        self.build()
        for line in ProductLine.objects.filter(is_active=True):
            with self.subTest(line=line.slug):
                self.assertIsNotNone(
                    self.read("services", line.slug, "index.html"))

    def test_every_active_service_gets_a_page(self):
        self.build()
        for service in self.line.services.filter(is_active=True):
            with self.subTest(service=service.slug):
                html = self.read("services", self.line.slug, service.slug,
                                 "index.html")
                self.assertIsNotNone(html)
                self.assertIn(service.name, html)

    def test_a_deactivated_line_stops_being_served(self):
        """Overwriting can't delete, so turning a line off has to actually take
        its page down."""
        self.build()
        self.assertIsNotNone(self.read("services", self.line.slug, "index.html"))

        self.line.is_active = False
        self.line.save(update_fields=["is_active"])
        self.build()
        self.assertIsNone(self.read("services", self.line.slug, "index.html"))

    def test_a_deactivated_service_stops_being_served(self):
        service = self.line.services.first()
        self.build()
        self.assertIsNotNone(
            self.read("services", self.line.slug, service.slug, "index.html"))

        service.is_active = False
        service.save(update_fields=["is_active"])
        self.build()
        self.assertIsNone(
            self.read("services", self.line.slug, service.slug, "index.html"))

    # --- what a crawler needs ---
    def test_each_page_carries_a_title_description_and_canonical(self):
        self.build()
        service = self.line.services.first()
        html = self.read("services", self.line.slug, service.slug, "index.html")
        self.assertIn(f"<title>{service.name}", html)
        self.assertIn('<meta name="description"', html)
        self.assertIn(
            f'<link rel="canonical" href="https://example.test/services/'
            f'{self.line.slug}/{service.slug}">', html)

    def test_the_meta_description_is_not_truncated_mid_word(self):
        long_text = "A very long description. " * 20
        service = self.line.services.first()
        service.description = long_text[:200]
        service.save(update_fields=["description"])
        self.build()
        html = self.read("services", self.line.slug, service.slug, "index.html")
        match = re.search(r'<meta name="description" content="([^"]*)"', html)
        content = match.group(1)
        self.assertLessEqual(len(content), 160)
        self.assertFalse(content.rstrip("…").endswith(" "))

    def test_the_structured_data_is_valid_json(self):
        self.build()
        html = self.read("services", self.line.slug, "index.html")
        block = re.search(
            r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
        data = json.loads(block.group(1))
        self.assertEqual(data["@type"], "Service")
        self.assertEqual(data["provider"]["@type"], "Organization")

    def test_no_template_syntax_leaks_into_the_output(self):
        """A multi-line `{# #}` renders literally — it happened once."""
        self.build()
        for path in self.out.rglob("*.html"):
            with self.subTest(path=path.name):
                body = path.read_text()
                for token in ("{#", "{%", "{{"):
                    self.assertNotIn(token, body)

    def test_a_sitemap_and_robots_are_written(self):
        self.build()
        sitemap = self.read("sitemap.xml")
        self.assertIn("<urlset", sitemap)
        self.assertIn(f"https://example.test/services/{self.line.slug}", sitemap)

        robots = self.read("robots.txt")
        self.assertIn("Allow: /services/", robots)
        self.assertIn("Disallow: /earnings", robots)
        self.assertIn("Sitemap: https://example.test/sitemap.xml", robots)

    def test_the_signed_in_app_is_kept_out_of_the_index(self):
        self.build()
        robots = self.read("robots.txt")
        for private in ("/projects/", "/board", "/work", "/people", "/company"):
            with self.subTest(path=private):
                self.assertIn(f"Disallow: {private}", robots)


class NoTalentDirectoryTests(TestCase):
    """The restraint that keeps these pages from becoming a marketplace."""

    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "pplead@ril.team", "x", full_name="Ngozi Adeyemi",
            role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.expert = User.objects.create_user(
            "ppexpert@ril.dev", "x", full_name="Zainab Bello",
            role=User.Role.EXPERT, lead=self.lead)
        self.expert.product_lines.add(self.line)
        self.customer = User.objects.create_user(
            "ppclient@acme.io", "x", full_name="Amara Okafor",
            company="HopeBridge", role=User.Role.CLIENT)
        call_command("build_service_pages", out=str(self.out),
                     site="https://example.test", clean=True)

    def all_html(self):
        return "\n".join(p.read_text() for p in self.out.rglob("*.html"))

    def test_no_expert_is_named(self):
        self.assertNotIn("Zainab Bello", self.all_html())

    def test_no_delivery_lead_is_named(self):
        self.assertNotIn("Ngozi Adeyemi", self.all_html())

    def test_no_client_is_named(self):
        html = self.all_html()
        self.assertNotIn("Amara Okafor", html)
        self.assertNotIn("HopeBridge", html)

    def test_no_headcount_is_published(self):
        """"Our 47 designers" is a talent directory with the names removed."""
        html = self.all_html().lower()
        for phrase in ("experts available", "designers", "freelancers",
                       "our team of"):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, html)


class AnonymisedStatsTests(TestCase):
    """A number drawn from three projects describes three clients, not a line."""

    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.customer = User.objects.create_user(
            "stclient@acme.io", "x", role=User.Role.CLIENT)

    def deliver(self, n, days=10):
        for i in range(n):
            created = timezone.now() - timedelta(days=days + 1)
            project = Project.objects.create(
                title=f"Brief {i}", client=self.customer,
                category="Brand identity", description="…",
                product_line=self.line, stage=Project.Stage.COMPLETED,
                quote_usd=1000, completed_at=timezone.now())
            Project.objects.filter(id=project.id).update(created_at=created)

    def test_below_the_threshold_nothing_is_published(self):
        self.deliver(public.MIN_SAMPLE - 1)
        self.assertEqual(public.line_stats(self.line), {})

    def test_at_the_threshold_it_is(self):
        self.deliver(public.MIN_SAMPLE)
        stats = public.line_stats(self.line)
        self.assertEqual(stats["delivered_count"], public.MIN_SAMPLE)
        self.assertEqual(stats["avg_days"], 11)

    def test_a_line_with_no_history_says_nothing(self):
        self.assertEqual(public.line_stats(self.line), {})

    def test_the_page_omits_the_block_entirely_when_there_is_nothing(self):
        out = Path(tempfile.mkdtemp())
        call_command("build_service_pages", out=str(out),
                     site="https://example.test", clean=True)
        html = (out / "services" / self.line.slug / "index.html").read_text()
        self.assertNotIn("projects delivered", html)

    def test_the_page_shows_them_once_there_are_enough(self):
        self.deliver(public.MIN_SAMPLE + 3)
        out = Path(tempfile.mkdtemp())
        call_command("build_service_pages", out=str(out),
                     site="https://example.test", clean=True)
        html = (out / "services" / self.line.slug / "index.html").read_text()
        self.assertIn("projects delivered", html)


class ServiceSlugTests(TestCase):
    def test_a_slug_is_derived_from_the_name(self):
        line = ProductLine.objects.get(slug="design-creative")
        service = Service.objects.create(
            product_line=line, name="Pitch Deck Design")
        self.assertEqual(service.slug, "pitch-deck-design")

    def test_an_explicit_slug_is_kept(self):
        line = ProductLine.objects.get(slug="design-creative")
        service = Service.objects.create(
            product_line=line, name="Pitch Deck Design", slug="decks")
        self.assertEqual(service.slug, "decks")

    def test_the_seeded_catalogue_all_has_slugs(self):
        """The backfill migration's job — without it, every page is skipped."""
        missing = Service.objects.filter(slug="").count()
        self.assertEqual(missing, 0)


class BuildOrderTests(TestCase):
    """`vite build` empties its output directory.

    Generating these pages before the frontend build therefore throws them
    away silently, which is exactly the kind of deployment mistake that is
    invisible until a crawler reports the app shell.
    """

    def test_it_warns_when_the_frontend_is_not_built(self):
        from io import StringIO

        out = Path(tempfile.mkdtemp())
        err = StringIO()
        call_command("build_service_pages", out=str(out),
                     site="https://example.test", stderr=err)
        self.assertIn("doesn't look built", err.getvalue())

    def test_it_is_quiet_when_the_frontend_is_built(self):
        from io import StringIO

        out = Path(tempfile.mkdtemp())
        (out / "index.html").write_text("<html></html>")
        err = StringIO()
        call_command("build_service_pages", out=str(out),
                     site="https://example.test", stderr=err)
        self.assertNotIn("doesn't look built", err.getvalue())
