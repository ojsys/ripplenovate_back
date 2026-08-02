"""The starting catalogue of product lines and services.

Used by the data migration that creates them and by `manage.py seed`. Editing
this file does not change an existing install — lines and services are data, and
are maintained in the Django admin once they exist.
"""

# (slug, name, tagline, accent, icon, [services])
# A service is (name, description, typical_timeline).
PRODUCT_LINES = [
    (
        "software-web", "Software & Web",
        "Web and mobile products, APIs, automation and AI.",
        "#0FA37F", "grid",
        [
            ("Web application", "A custom web app or internal tool.", "4–8 weeks"),
            ("Website", "Marketing site, landing pages, CMS.", "2–4 weeks"),
            ("Mobile app", "iOS / Android, or cross-platform.", "6–12 weeks"),
            ("API & integrations", "Connect systems, build or consume APIs.", "2–6 weeks"),
            ("Automation & AI", "Workflow automation and AI integration.", "2–6 weeks"),
        ],
    ),
    (
        "design-creative", "Design & Creative",
        "Product design, brand identity, graphics and motion.",
        "#7C3AED", "user",
        [
            ("UI/UX design", "Product interface design, flows and prototypes.", "2–6 weeks"),
            ("UX audit & research", "Usability review, user interviews, findings.", "1–3 weeks"),
            ("Brand identity", "Logo, palette, type, and a usage guide.", "2–5 weeks"),
            ("Graphic design", "Social, print, campaign and marketing assets.", "1–3 weeks"),
            ("Presentation & pitch decks", "Investor, sales and report decks.", "1–2 weeks"),
            ("Motion & video", "Explainers, animation, video editing.", "2–4 weeks"),
        ],
    ),
    (
        "data-research", "Data & Research",
        "Analysis, dashboards, market research and evaluation.",
        "#2563EB", "list",
        [
            ("Data analysis", "Clean, analyse and interpret a dataset.", "1–4 weeks"),
            ("Dashboards & BI", "Reporting dashboards and data pipelines.", "2–6 weeks"),
            ("Market research", "Sizing, competitor and customer research.", "2–4 weeks"),
            ("Monitoring & evaluation", "M&E frameworks, indicators, reporting.", "3–6 weeks"),
            ("Literature review", "Structured desk research and synthesis.", "1–3 weeks"),
        ],
    ),
    (
        "content-comms", "Content & Communications",
        "Copy, content, social media and technical writing.",
        "#B4791A", "check",
        [
            ("Copywriting", "Website, product and campaign copy.", "1–2 weeks"),
            ("Social media management", "Content calendar, posts, community.", "Monthly"),
            ("Technical writing", "Docs, guides, API references.", "2–4 weeks"),
            ("Grant & proposal writing", "Funding applications and concept notes.", "2–4 weeks"),
        ],
    ),
    (
        "virtual-operations", "Virtual Operations",
        "Virtual assistance, admin, CRM and customer support.",
        "#0B7D61", "users",
        [
            ("Virtual assistance", "Scheduling, inbox, travel, admin support.", "Monthly"),
            ("CRM & admin support", "Data entry, CRM hygiene, process admin.", "Monthly"),
            ("Customer support", "Inbox, chat and ticket handling.", "Monthly"),
            ("Bookkeeping", "Reconciliation, invoicing, basic reporting.", "Monthly"),
        ],
    ),
]

# The six free-text categories the client form used before product lines existed.
# Maps each onto (product line slug, service name) so old briefs land somewhere
# sensible instead of being orphaned.
LEGACY_CATEGORY_MAP = {
    "Software development": ("software-web", "Web application"),
    "Website development": ("software-web", "Website"),
    "AI integration & automation": ("software-web", "Automation & AI"),
    "Data analysis": ("data-research", "Data analysis"),
    "Design & branding": ("design-creative", "Brand identity"),
    "Research & intelligence": ("data-research", "Market research"),
}
