"""The taxonomy of work the platform delivers.

A **Product Line** is a discipline — software, design, research — with its own
delivery leads and experts. A **Service** is a concrete offering inside a line,
and it is what a client actually picks when posting a brief.

This lives in its own app on purpose: `accounts` (which lines a person works in)
and `projects` (which line a brief belongs to) both point here, so keeping the
taxonomy separate leaves the app dependencies one-way.

Lines are data, not code. Opening a new discipline is an admin action — create
the line, set `is_active` — not a deploy.
"""
from django.db import models


class ProductLine(models.Model):
    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(max_length=80, unique=True)
    tagline = models.CharField(
        max_length=140, blank=True,
        help_text="One line shown on the product-line card when a client is choosing.",
    )
    description = models.TextField(blank=True)
    accent = models.CharField(
        max_length=7, default="#0FA37F",
        help_text="Hex colour for this line's chip and card, e.g. #7C3AED.",
    )
    icon = models.CharField(
        max_length=40, blank=True,
        help_text="Icon key from the frontend's icon set (e.g. 'grid', 'check').",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive lines are hidden from clients and can't take new briefs. "
                  "Turn a line off rather than deleting it — existing projects keep "
                  "pointing at it.",
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class Service(models.Model):
    """A concrete offering inside a line — what the client picks on the brief form."""

    product_line = models.ForeignKey(
        ProductLine, on_delete=models.CASCADE, related_name="services"
    )
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=200, blank=True)
    typical_timeline = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["product_line", "name"], name="unique_service_per_line"
            )
        ]

    def __str__(self):
        return f"{self.product_line.name} · {self.name}"
