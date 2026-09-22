import base64
import frappe
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01 as Vapid


def run():
    settings = frappe.get_single("Sofia Push Settings")
    pem = settings.get_password("vapid_private_key", raise_exception=False)
    print("PEM original, primeros 40:", repr(pem[:40]))

    key = serialization.load_pem_private_key(pem.encode(), password=None)
    der = key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    der_b64url = base64.urlsafe_b64encode(der).decode().rstrip("=")
    print("DER base64url, longitud:", len(der_b64url), "primeros 30:", der_b64url[:30])

    # Verificar que pywebpush/py-vapid puede leerlo de vuelta antes de guardar.
    v = Vapid.from_string(der_b64url)
    print("Vapid.from_string OK, public numbers coinciden con la key original:",
          v.private_key.public_key().public_numbers() == key.public_key().public_numbers())

    settings.vapid_private_key = der_b64url
    settings.save(ignore_permissions=True)
    frappe.db.commit()
    print("guardado OK — public key SIN TOCAR:", settings.vapid_public_key)
