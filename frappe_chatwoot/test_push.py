import frappe
from frappe_chatwoot.frappe_chatwoot.api.push import notify_user

def run():
    notify_user(
        "alejandro.moreno@lavendi.mx",
        "Prueba de Sofía",
        "Si ves esto, el push ya funciona en tu teléfono.",
        url="/crm/conversaciones",
        tag="test-push-20260920",
    )
    print("enviado")
