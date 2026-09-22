import frappe

def run():
    rows = frappe.get_all(
        "Error Log",
        fields=["name", "creation", "error"],
        order_by="creation desc",
        limit_page_length=8,
    )
    for r in rows:
        print("===", r.name, r.creation)
        print((r.error or "")[:400])
