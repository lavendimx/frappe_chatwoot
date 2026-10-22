# Copyright (c) 2026, lavendi.mx
# License: MIT
"""DND (do-not-disturb) por contacto.

Bloquea SOLO lo automático: secuencias de seguimiento, autofollowup de
reactivación y campañas de email. NO bloquea al agente de ventas en una
conversación viva ni a un humano que responde a mano — eso no es lo que
"no contactar" pide (Alejandro, 2026-10-22: "que no reciba una secuencia o un
mensaje automático"). Bloquear también la conversación en vivo sería otra
feature (silenciar al contacto), no se pidió.

Campañas de email no tiene gancho propio por contacto (`campana_email.py`
delega el 100% del envío a `Newsletter.send_emails()`, framework nativo de
Frappe): en vez de inventar uno, `activar()` asegura un `Email Unsubscribe`
global para el correo del contacto — ya es el mecanismo que Frappe respeta
solo al mandar un newsletter (mismo doctype que ya usa
`secuencias._dado_de_baja` para la baja por-secuencia).

Dos orígenes posibles, solo para el registro (`origen` en el log, no cambia
el efecto):
    - "manual": un vendedor lo marca desde el panel (dropdown del contacto
      en `Deal.vue`).
    - "cta": detección automática — el contacto respondió la opción de
      "detener" del CTA numerado de un paso de secuencia (`Secuencia
      Paso.opcion_detener`), ver `secuencias._debe_salir`.

Reactivar (`desactivar`) NO reinscribe en nada: quitar el flag solo permite
que el contacto vuelva a ser candidato. Reinscribirlo es una acción humana
aparte (`secuencias.inscribir_deal`) — a propósito, para no reabrir contacto
con alguien que pidió baja sin que un humano lo decida.
"""

import frappe
from frappe import _

_ROLES_EDICION = ("System Manager", "Sales Manager")


def _exigir_edicion():
    if frappe.session.user == "Administrator":
        return
    roles = frappe.get_roles(frappe.session.user)
    if not any(r in roles for r in _ROLES_EDICION):
        frappe.throw(_("No tienes permiso para cambiar el estado de contacto."),
                     frappe.PermissionError)


def en_dnd(contacto: str | None) -> bool:
    """¿Este contacto tiene el DND activo? `contacto` es el `name` del
    doctype `Contact` (mismo valor que `Secuencia Inscripcion.contacto`,
    `Chatwoot Followup`'s deal.contact, etc.)."""
    if not contacto:
        return False
    return bool(frappe.db.get_value("Contact", contacto, "no_contactar"))


def _email_de(contacto: str) -> str | None:
    return frappe.db.get_value("Contact", contacto, "email_id")


def _asegurar_unsubscribe(email: str) -> None:
    if frappe.db.exists("Email Unsubscribe", {"email": email, "global_unsubscribe": 1}):
        return
    frappe.get_doc({
        "doctype": "Email Unsubscribe",
        "email": email,
        "global_unsubscribe": 1,
    }).insert(ignore_permissions=True)


def _cancelar_inscripciones_activas(contacto: str, motivo: str) -> int:
    # Import diferido: secuencias.py importa este módulo a nivel de módulo
    # (ciclo si se importara arriba). Debe pasar por _set_ins(), el único
    # punto que refleja el cambio en CRM Deal — ver secuencias.py:393-395.
    from frappe_chatwoot.utils import secuencias

    activas = frappe.get_all(
        "Secuencia Inscripcion", filters={"contacto": contacto, "estado": "Activa"},
        pluck="name",
    )
    for nombre in activas:
        secuencias._set_ins(
            nombre,
            {"estado": "Salió por cambio de etapa", "motivo": motivo[:280]},
            update_modified=False,
        )
    return len(activas)


def _activar_interno(contacto: str, motivo: str | None = None, origen: str = "manual") -> dict:
    """Sin chequeo de rol — para llamar desde el motor (scheduler), que ya
    corre con el mismo nivel de confianza que `secuencias.avanzar()`."""
    if not frappe.db.exists("Contact", contacto):
        frappe.throw(_("Ese contacto no existe"))
    motivo = (motivo or "Marcado a mano desde el panel").strip() or "Marcado a mano desde el panel"
    frappe.db.set_value("Contact", contacto, "no_contactar", 1)
    canceladas = _cancelar_inscripciones_activas(contacto, f"DND ({origen}): {motivo}")
    email = _email_de(contacto)
    if email:
        _asegurar_unsubscribe(email)
    frappe.db.commit()
    return {"ok": True, "contacto": contacto, "inscripciones_canceladas": canceladas}


@frappe.whitelist()
def activar(contacto: str, motivo: str | None = None) -> dict:
    """Prende el DND a mano desde el panel (botón del dropdown de contacto)."""
    _exigir_edicion()
    return _activar_interno(contacto, motivo, origen="manual")


@frappe.whitelist()
def desactivar(contacto: str) -> dict:
    _exigir_edicion()
    if not frappe.db.exists("Contact", contacto):
        frappe.throw(_("Ese contacto no existe"))
    frappe.db.set_value("Contact", contacto, "no_contactar", 0)
    frappe.db.commit()
    return {"ok": True, "contacto": contacto}
