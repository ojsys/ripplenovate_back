"""Emit static, crawlable pages for every active service.

The platform has no organic acquisition surface at all: no directory, no
indexable pages, nothing for a search engine to find. Every client has to be
hand-carried in by a business developer, which makes growth entirely gated on
BD headcount.

These pages fix that without becoming a marketplace. They sell the **outcome** —
a fixed price, a vetted team, a date — and they contain no expert profiles, no
names and no headcounts. That restraint is the point: a public talent directory
is precisely the surface this platform deliberately doesn't have.

Generated from the catalogue in the database rather than fetched from the API at
build time, so there is one authoritative source and no chicken-and-egg between
the build and a running server.

    python manage.py build_service_pages --out ../frontend/dist

Writes `services/<line>/index.html`, `services/<line>/<service>/index.html`,
`sitemap.xml` and `robots.txt`.

**The web server must serve these ahead of the SPA fallback.** Without a rule
for `/services/*`, the single-page app's catch-all answers first and a crawler
sees the empty shell — see DEPLOYMENT.md.
"""
import json
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from accounts.models import SiteSettings
from catalog import public
from catalog.models import ProductLine


class Command(BaseCommand):
    help = "Generate static, indexable pages for the public service catalogue."

    def add_arguments(self, parser):
        parser.add_argument(
            "--out", default="../frontend/dist",
            help="Directory to write into — normally the built frontend.",
        )
        parser.add_argument(
            "--site", default=None,
            help="Public base URL. Defaults to FRONTEND_URL.",
        )
        parser.add_argument(
            "--clean", action="store_true",
            help="Remove the existing services/ tree first, so a line that has "
                 "been deactivated stops being served.",
        )

    def handle(self, *args, **options):
        out = Path(options["out"]).resolve()
        site = (options["site"] or settings.FRONTEND_URL).rstrip("/")
        branding = SiteSettings.load()

        services_root = out / "services"
        if options["clean"] and services_root.exists():
            # Deactivating a line has to actually take its page down, and
            # overwriting can't delete. Off by default so a normal build never
            # blows away something unexpected.
            shutil.rmtree(services_root)

        out.mkdir(parents=True, exist_ok=True)
        # `vite build` empties its output directory, so generating these before
        # the frontend build silently throws them away. Warning rather than
        # refusing: the output directory is a flag, and somebody may well be
        # writing somewhere else on purpose.
        if not (out / "index.html").exists():
            self.stderr.write(self.style.WARNING(
                f"  {out} has no index.html — the frontend doesn't look built.\n"
                "  Build it FIRST: `vite build` empties this directory and would "
                "wipe these pages."))
        lines = (ProductLine.objects.filter(is_active=True)
                 .prefetch_related("services").order_by("order", "name"))

        urls, written = [], 0
        for line in lines:
            services = [s for s in line.services.all() if s.is_active]
            written += self._write(
                services_root / line.slug / "index.html",
                self._line_context(line, services, site, branding),
            )
            urls.append(f"{site}/services/{line.slug}")

            for service in services:
                if not service.slug:
                    self.stderr.write(self.style.WARNING(
                        f"  skipped “{service.name}” — no slug. Run migrations."))
                    continue
                written += self._write(
                    services_root / line.slug / service.slug / "index.html",
                    self._service_context(line, service, site, branding),
                )
                urls.append(f"{site}/services/{line.slug}/{service.slug}")

        self._write_text(out / "sitemap.xml", self._sitemap(site, urls))
        self._write_text(out / "robots.txt", self._robots(site))

        self.stdout.write(self.style.SUCCESS(
            f"Wrote {written} page(s) and a sitemap of {len(urls)} URL(s) to {out}"))
        self.stdout.write(
            "Re-run this after any frontend rebuild — `vite build` clears the "
            "directory.")

    # --- contexts ---

    def _base(self, line, site, branding):
        return {
            "site": site,
            "brand": branding.brand_name,
            "tagline": branding.tagline,
            "accent": line.accent or "#0FA37F",
            "line": line,
        }

    def _line_context(self, line, services, site, branding):
        lede = (line.tagline
                or f"Fixed-price {line.name.lower()} delivered by vetted expert "
                   "teams. One quote, one accountable lead, no bidding.")
        context = self._base(line, site, branding)
        context.update({
            "page_title": f"{line.name} — fixed-price delivery | {branding.brand_name}",
            "meta_description": self._clip(lede),
            "canonical": f"{site}/services/{line.slug}",
            "eyebrow": "Product line",
            "heading": line.name,
            "lede": lede,
            "description": line.description,
            "services": services,
            "timeline": "",
            "stats": public.line_stats(line),
            "parent": False,
        })
        context["structured_data"] = self._json_ld(
            context["heading"], context["meta_description"],
            context["canonical"], branding, line)
        return context

    def _service_context(self, line, service, site, branding):
        lede = (service.description
                or f"{service.name} delivered at a fixed price by a vetted "
                   f"{line.name.lower()} team.")
        context = self._base(line, site, branding)
        context.update({
            "page_title": f"{service.name} — fixed price | {branding.brand_name}",
            "meta_description": self._clip(lede),
            "canonical": f"{site}/services/{line.slug}/{service.slug}",
            "eyebrow": line.name,
            "heading": service.name,
            "lede": lede,
            "description": "",
            "services": [],
            "timeline": service.typical_timeline,
            "stats": public.service_stats(service),
            "parent": True,
        })
        context["structured_data"] = self._json_ld(
            context["heading"], context["meta_description"],
            context["canonical"], branding, line)
        return context

    # --- output ---

    def _json_ld(self, name, description, url, branding, line):
        """Service markup, deliberately not Person or employee markup."""
        return json.dumps({
            "@context": "https://schema.org",
            "@type": "Service",
            "name": name,
            "description": description,
            "url": url,
            "serviceType": line.name,
            "provider": {"@type": "Organization", "name": branding.brand_name},
            "areaServed": "Worldwide",
        }, indent=2)

    def _clip(self, text, limit=155):
        """A meta description search engines won't truncate mid-word."""
        text = " ".join(strip_tags(text or "").split())
        if len(text) <= limit:
            return text
        return text[:limit].rsplit(" ", 1)[0] + "…"

    def _write(self, path, context):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_to_string("public/service_page.html", context), encoding="utf-8")
        return 1

    def _write_text(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def _sitemap(self, site, urls):
        entries = "\n".join(
            f"  <url><loc>{u}</loc></url>" for u in [f"{site}/"] + urls)
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                f"{entries}\n</urlset>\n")

    def _robots(self, site):
        # The app itself is behind a login and has nothing to index; the public
        # pages and the landing page do.
        return (
            "User-agent: *\n"
            "Allow: /$\n"
            "Allow: /services/\n"
            "Disallow: /projects/\n"
            "Disallow: /board\n"
            "Disallow: /work\n"
            "Disallow: /earnings\n"
            "Disallow: /people\n"
            "Disallow: /company\n"
            "Disallow: /retainers\n"
            f"\nSitemap: {site}/sitemap.xml\n"
        )
