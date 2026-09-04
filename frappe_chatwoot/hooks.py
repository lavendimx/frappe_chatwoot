from . import __version__ as app_version

app_name = "frappe_chatwoot"
app_title = "Frappe Chatwoot"
app_publisher = "Hypedrive"
app_description = "Live-read Chatwoot conversation integration for Frappe. Chatwoot stays the system of record — no message duplication."
app_email = "9.shivamgupta.6@gmail.com"
app_license = "MIT"

# No custom Desk-wide JS needed today — the CRM-side integration (if/when the
# tab-embedded contract lands) lives inside the CRM frontend bundle itself,
# exactly like frappe_whatsapp's own app_include_js is unrelated to CRM's
# WhatsApp tab (that tab is compiled into CRM's own frontend, not loaded via
# this hook). Left here, commented, as the documented extension point:
# app_include_js = "/assets/frappe_chatwoot/js/frappe_chatwoot.js"

# ---------------------------------------------------------------------------
# Scheduler events
# ---------------------------------------------------------------------------
# Chatwoot's ActionCable feed is the "live" signal (see utils/realtime_bridge.py
# for why a persistent WS connection is NOT run as a bench worker in this
# deployment). Short-interval cron poll is the fallback that keeps
# publish_realtime signals flowing without a long-lived process. See the
# module docstring in realtime_bridge.py for the full tradeoff writeup.
scheduler_events = {
    "cron": {
        # every minute — cheap: HEAD-weight conversations list call, cached
        # response compared by (conversation_id, updated_at) to detect deltas.
        "* * * * *": [
            "frappe_chatwoot.utils.realtime_bridge.poll_and_broadcast",
        ],
    },
}

# ---------------------------------------------------------------------------
# doc_events
# ---------------------------------------------------------------------------
# KB Source is a site-level custom doctype (Sofia RAG pipeline), not owned by
# this app — hooked here anyway since frappe_chatwoot is the glue-code app for
# the Sofia platform. before_insert forces inbox_id from the user's own
# User Permission, ignoring whatever a client-facing Web Form submission sent —
# the real tenant-isolation guarantee lives here, not in the form config.
doc_events = {
    "KB Source": {
        "before_insert": "frappe_chatwoot.utils.kb_isolation.set_inbox_from_user_permission",
    },
}

# ---------------------------------------------------------------------------
# Fixtures / boilerplate hook surface — none needed today.
# ---------------------------------------------------------------------------
