"""Client reviews, and the line between the private ones and the public ones.

Every review is written under an explicit promise: *this goes to your delivery
lead and nobody else*. That promise is on the form, in the summary card and in
the help page, so it is what the words were written under — and consent given
for one audience is not consent for another.

So this module has two jobs, and keeping them apart is the whole point:

**`published`** — only reviews whose author ticked "you may quote this". Shown
on the landing page and the public service pages. Attributed to the company and
the discipline, never to a person, and never naming the expert or lead who
delivered it: a public talent directory is the surface this platform
deliberately doesn't have.

**`summary`** — an honest aggregate over *every* review, consented or not.
Separate from the quotes above it precisely because a testimonial wall is
understood to be curated, and an average is not. Publishing "4.9 stars" derived
only from the reviews we chose to show would be a lie of exactly the kind this
codebase spends a lot of effort avoiding elsewhere.
"""
from decimal import ROUND_HALF_UP, Decimal

from .models import ProjectFeedback, Project

# Below this, an average describes a handful of individual clients rather than
# the platform — the same threshold the public service pages use.
MIN_SAMPLE = 5


def _round(value):
    return float(Decimal(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def published(limit=12, line_slug=None):
    """Consented reviews, newest first, safe to show anybody.

    A review with no comment is skipped — a bare star rating is not a
    testimonial, and a card containing only a number reads as filler.
    """
    rows = (ProjectFeedback.objects
            .filter(may_publish=True)
            .exclude(comment="")
            .select_related("project", "project__organisation",
                            "project__product_line"))
    if line_slug:
        rows = rows.filter(project__product_line__slug=line_slug)

    out = []
    for row in rows[:limit]:
        project = row.project
        line = project.product_line
        out.append({
            "id": row.id,
            "rating": row.rating,
            "comment": row.comment,
            # The company, not the person. Enough to be credible, and exactly
            # what the consent copy says will be shown.
            "company": (project.organisation.name if project.organisation_id
                        else project.company) or "A client",
            "product_line": line.name if line else "",
            "accent": line.accent if line else "#0FA37F",
            "service": project.category,
            "created_at": row.created_at,
        })
    return out


def summary(line_slug=None):
    """The honest average, across every review rather than the published ones.

    Returns an empty dict below the sample threshold, so a caller can render
    whatever is present without having to decide what's safe to show.
    """
    rows = ProjectFeedback.objects.all()
    if line_slug:
        rows = rows.filter(project__product_line__slug=line_slug)

    ratings = list(rows.values_list("rating", flat=True))
    if len(ratings) < MIN_SAMPLE:
        return {}

    repeat = [r for r in rows.values_list("would_work_again", flat=True)
              if r is not None]
    out = {
        "average": _round(sum(ratings) / len(ratings)),
        "count": len(ratings),
    }
    if len(repeat) >= MIN_SAMPLE:
        out["would_repeat_percent"] = round(
            sum(1 for r in repeat if r) / len(repeat) * 100)
    return out
