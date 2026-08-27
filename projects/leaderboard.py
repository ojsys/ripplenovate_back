"""Who is actually delivering well, ranked honestly.

This is a real change of posture and worth naming. The platform was built so a
client never chooses their expert — that's what removes the bidding, the
shortlisting and the sales burden, and it's the structural reason work doesn't
drift off-platform. A public leaderboard moves some of that choice back to the
client, deliberately.

Three rules keep it from becoming a vanity metric:

**A floor before anybody appears.** Somebody with one delivered project and one
five-star review is not the best on the platform, they're the luckiest. Below
the threshold you simply don't appear — which also protects a new lead from
being ranked bottom on a sample of two.

**Every component is shown, not just the score.** A ranking whose inputs are
hidden is a thing people game rather than trust. The rating, the on-time rate
and the delivered count all travel with the row.

**Nulls stay null.** Somebody nobody has rated shows no rating, not a zero.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.contrib.auth import get_user_model
from django.db.models import Q

from payments.models import Earning

from .models import Project, ProjectFeedback

User = get_user_model()

# Delivered projects needed before anybody is ranked at all.
MIN_DELIVERED = 3
# Reviews needed before a rating is published beside them.
MIN_RATINGS = 3


def _round(value, places="0.1"):
    return float(Decimal(value).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _score(delivered, rating, on_time):
    """A single number for ordering, from parts that are all on display.

    Weighted toward what a client actually asked about: were people happy, did
    it arrive when promised, and has this person done it enough times to mean
    something. Volume is deliberately the weakest of the three — otherwise the
    leaderboard just ranks whoever has been here longest.
    """
    score = 0.0
    if rating is not None:
        score += (rating / 5) * 60
    if on_time is not None:
        score += (on_time / 100) * 25
    # Flattens quickly: the gap between 3 and 10 projects matters, the gap
    # between 40 and 60 does not.
    score += min(delivered, 20) / 20 * 15
    return _round(score, "0.01")


def _rating_for(project_ids):
    ratings = list(ProjectFeedback.objects
                   .filter(project_id__in=project_ids)
                   .values_list("rating", flat=True))
    if len(ratings) < MIN_RATINGS:
        return None, len(ratings)
    return _round(sum(ratings) / len(ratings)), len(ratings)


def _punctuality(projects):
    flags = [p.is_on_time for p in projects if p.is_on_time is not None]
    if not flags:
        return None
    return round(sum(1 for f in flags if f) / len(flags) * 100)


def leads(limit=10, line_slug=None, public=True):
    """Delivery leads, best first.

    `public=False` drops the opt-out filter — an admin looking at their own
    platform should see everybody, including the people who asked not to be
    listed outside it.
    """
    qs = User.objects.filter(role=User.Role.DELIVERY_LEAD, is_active=True)
    if public:
        qs = qs.filter(show_in_leaderboard=True)

    rows = []
    for person in qs.prefetch_related("product_lines"):
        delivered = list(Project.objects.filter(
            lead=person, stage=Project.Stage.COMPLETED,
            **({"product_line__slug": line_slug} if line_slug else {}),
        ))
        if len(delivered) < MIN_DELIVERED:
            continue
        rating, sample = _rating_for([p.id for p in delivered])
        on_time = _punctuality(delivered)
        rows.append({
            "id": person.id,
            "name": person.full_name or person.email.split("@")[0],
            "initials": person.initials,
            "specialty": person.specialty,
            "product_lines": [
                {"slug": l.slug, "name": l.name, "accent": l.accent}
                for l in person.product_lines.all()
            ],
            "delivered": len(delivered),
            "rating": rating,
            "rating_sample": sample,
            "on_time_percent": on_time,
            "score": _score(len(delivered), rating, on_time),
        })

    rows.sort(key=lambda r: -r["score"])
    return rows[:limit]


def experts(limit=10, line_slug=None, public=True):
    """Experts, best first.

    Ranked on the work they were actually paid for rather than on projects they
    were merely attached to — an expert on a five-person team didn't deliver
    five people's worth of it.
    """
    qs = User.objects.filter(role=User.Role.EXPERT, is_active=True)
    if public:
        qs = qs.filter(show_in_leaderboard=True)

    rows = []
    for person in qs.prefetch_related("product_lines"):
        delivered = list(Project.objects.filter(
            Q(experts=person) | Q(expert=person),
            stage=Project.Stage.COMPLETED,
            **({"product_line__slug": line_slug} if line_slug else {}),
        ).distinct())
        if len(delivered) < MIN_DELIVERED:
            continue
        rating, sample = _rating_for([p.id for p in delivered])
        on_time = _punctuality(delivered)
        # Approved task payments — evidence of work signed off, not just
        # attendance.
        approved = Earning.objects.filter(
            user=person, kind=Earning.Kind.EXPERT).count()
        rows.append({
            "id": person.id,
            "name": person.full_name or person.email.split("@")[0],
            "initials": person.initials,
            "specialty": person.specialty,
            "product_lines": [
                {"slug": l.slug, "name": l.name, "accent": l.accent}
                for l in person.product_lines.all()
            ],
            "delivered": len(delivered),
            "approved_payments": approved,
            "rating": rating,
            "rating_sample": sample,
            "on_time_percent": on_time,
            "score": _score(len(delivered), rating, on_time),
        })

    rows.sort(key=lambda r: -r["score"])
    return rows[:limit]
