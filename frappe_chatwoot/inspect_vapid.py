import frappe

def run():
    settings = frappe.get_single("Sofia Push Settings")
    priv = settings.get_password("vapid_private_key", raise_exception=False)
    print("REPR primeros 60:", repr(priv[:60]) if priv else None)
    print("REPR ultimos 30:", repr(priv[-30:]) if priv else None)
    print("longitud:", len(priv) if priv else 0)
    print("contiene BEGIN:", "BEGIN" in (priv or ""))
