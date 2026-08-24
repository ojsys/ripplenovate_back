"""The admin half of the help page.

Kept on the server rather than in `frontend/src/guide.js` because everything in
that file ships in the JavaScript bundle and is readable by anyone who opens
devtools. Hiding the admin guide in the UI alone would have been a gesture, not
a boundary.

Nothing here is a secret in the sense that leaking it would let somebody *do*
anything — it's operational guidance, not credentials. But it names the admin
screens and describes what they can do, and there's no reason for a client or
an expert to be handed a map of them.

Same shape as the client-side entries so the page renders both identically:
a question, some paragraphs, an optional list, an optional note, an optional
link into the app.
"""

ADMIN_GUIDE = [
    {
        "id": "admin-queues",
        "section": "Running the platform",
        "q": "What needs my attention regularly?",
        "a": ["Four queues, all reachable from the menu."],
        "list": [
            "**Applications** — delivery leads and business developers waiting on review.",
            "**Verifications** — identity records waiting to be checked.",
            "**Refunds** — anything above the lead threshold needs your approval.",
            "**Payout requests** — on Earnings; nobody can settle their own.",
        ],
    },
    {
        "id": "admin-impersonate",
        "section": "Running the platform",
        "q": "Can I see what a user sees?",
        "a": [
            "Yes — **People**, find them, give a reason, and **View as**. You'll land in "
            "their account with a permanent banner until you stop.",
            "Every session is logged with the reason you gave. You can't change a "
            "password, repoint a payout account or request a withdrawal while you're "
            "in someone else's account.",
        ],
        "to": ["People", "/people"],
    },
    {
        "id": "admin-settings",
        "section": "Running the platform",
        "q": "Where do I change the numbers?",
        "a": ["Django admin → **Site settings**. The ones worth knowing about:"],
        "list": [
            "The **share percentages** — the platform is always the remainder, so "
            "setting the others moves it.",
            "**Refund reserve** — the slice of the platform's share set aside to fund refunds.",
            "**Days before a lead may close a silent client's project** — seven by default.",
            "**Minimum withdrawal**, and whether identity verification is required before payout.",
        ],
        "note": "Changes apply to future approvals only. Anything already paid keeps "
                "the split it was paid under.",
    },
    {
        "id": "admin-offboard",
        "section": "Running the platform",
        "q": "A delivery lead is leaving. What do I do?",
        "a": [
            "On **People**, find them and choose **Offboard**, then name who takes over. "
            "Their roster, live projects and active retainers all move across, and their "
            "experts are widened into the new lead's disciplines so nobody becomes "
            "unassignable.",
            "Completed projects and every earning stay exactly where they are. If they're "
            "owed money, you'll be told the balance — settling it is a separate decision, "
            "not part of the move.",
        ],
    },
    {
        "id": "admin-cycles",
        "section": "Running the platform",
        "q": "How do I know the retainer billing job is running?",
        "a": [
            "Django admin → **Cycle runs**. Every pass writes a row, including the ones "
            "that did nothing and including dry runs — an empty log is a much clearer "
            "answer to “did it fire?” than an absent one.",
            "It only raises a cycle in the week before a period starts, so most days "
            "doing nothing is the correct outcome.",
        ],
    },
    {
        "id": "admin-pages",
        "section": "Running the platform",
        "q": "How do the public service pages get updated?",
        "a": [
            "They're generated, not live. Run `build_service_pages` after any change to "
            "the catalogue — a new service, a renamed line, a line switched off.",
            "It has to run **after** the frontend build, because that empties the "
            "directory first. The command warns you if it looks like you've got the "
            "order wrong.",
        ],
    },
]
