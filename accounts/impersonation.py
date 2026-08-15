"""Signing in as another user, and the rules that keep it accountable.

A support tool: the fastest way to answer "it doesn't work for me" is to stand
where they're standing. It is also the single most dangerous capability in the
product — it makes an admin's actions indistinguishable from the account
holder's — so the whole feature is built around three constraints:

  * a token that *says* it is an impersonation, so no request can pretend
    otherwise, and that keeps saying so through refresh;
  * a short life, because a support session is minutes, not a fortnight;
  * a row in the audit log written before the token exists.

The claim is deliberately carried in the token rather than in a server-side
session: every request then arrives already knowing it is impersonated, with no
lookup that could be skipped by a view that forgot to ask.
"""
from datetime import timedelta

from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.tokens import RefreshToken

# Support work is measured in minutes. The ordinary 12h/14d pair exists so a
# real user isn't logged out mid-week; borrowing their account needs the
# opposite bias. An abandoned tab stops being a way in well before the day is
# out.
ACCESS_LIFETIME = timedelta(minutes=30)
REFRESH_LIFETIME = timedelta(hours=2)

# Claim names. `imp` is the impersonator's id — its presence is what marks a
# token as borrowed.
IMPERSONATOR_CLAIM = "imp"
EVENT_CLAIM = "imp_event"


def tokens_for_impersonation(admin, target, event_id):
    """Mint a short-lived token pair that acts as `target` but remembers `admin`.

    SimpleJWT copies every claim from a refresh token onto the access tokens it
    mints, including the ones minted by the refresh endpoint later. So `imp`
    survives a refresh, and an impersonated session can never quietly launder
    itself into an ordinary one by waiting for the access token to expire.
    """
    refresh = RefreshToken.for_user(target)
    refresh[IMPERSONATOR_CLAIM] = admin.id
    refresh[EVENT_CLAIM] = event_id
    refresh.set_exp(lifetime=REFRESH_LIFETIME)

    access = refresh.access_token
    access.set_exp(lifetime=ACCESS_LIFETIME)
    return {"access": str(access), "refresh": str(refresh)}


def impersonator_id(request):
    """The admin behind this request, or None if it's an ordinary session.

    Reads the validated token, so it cannot be spoofed by a header: the claim
    is inside a signature the client can't forge.
    """
    token = getattr(request, "auth", None)
    if token is None:
        return None
    try:
        return token.get(IMPERSONATOR_CLAIM)
    except AttributeError:
        return None


def event_id(request):
    token = getattr(request, "auth", None)
    if token is None:
        return None
    try:
        return token.get(EVENT_CLAIM)
    except AttributeError:
        return None


def is_impersonated(request):
    return impersonator_id(request) is not None


# Actions an admin must never take while wearing someone else's face. Not a
# security boundary against the admin — they are a superuser and could do the
# equivalent through the Django admin, where it is logged under their own name.
# It is a boundary against *ambiguity*: each of these leaves a record that
# says the user did something they did not do, and none of them has a support
# reason. Changing the password locks the real owner out of their own account;
# the other two move money.
def forbid_while_impersonating(request, what="This"):
    if is_impersonated(request):
        raise PermissionDenied(
            f"{what} can't be done while viewing someone else's account. "
            "Stop impersonating first and act as yourself."
        )
