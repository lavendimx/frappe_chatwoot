# Copyright (c) 2026, lavendi.mx
# License: MIT
"""
Autollena contacto / organización / oportunidad en una `CRM Task`.

Por qué existe
    `CRM Task` solo trae un vínculo (`reference_doctype`/`reference_docname`), así
    que en el panel de Tareas las tareas "parecen aisladas": no dicen de quién ni
    de qué tratan (reportado por Alejandro el 2026-09-15). Este módulo deriva el
    contacto, la organización y la oportunidad del registro al que la tarea está
    ligada y los guarda en campos propios, para poder verlos y filtrarlos.

No pisa lo que el humano puso
    Solo rellena los campos vacíos. Si alguien corrigió el contacto a mano, se
    respeta.

`Contact.company_name` y `CRM Lead.organization` son **texto libre** (Data), no
    un Link a `CRM Organization`; se ligan solo si existe una organización con ese
    nombre exacto, y si no se deja vacío en vez de inventar una referencia rota.
"""

import frappe

from frappe_chatwoot.frappe_chatwoot.api import push


def _org_por_nombre(nombre: str | None) -> str | None:
    if not nombre:
        return None
    return nombre if frappe.db.exists("CRM Organization", nombre) else None


def autollenar(doc, method=None):
    """doc_event `validate` de CRM Task. Nunca lanza: un fallo aquí no puede
    tumbar el guardado de la tarea."""
    ref_dt = doc.get("reference_doctype")
    ref_dn = doc.get("reference_docname")
    if not ref_dn:
        return
    try:
        if ref_dt == "CRM Deal":
            ref = frappe.db.get_value(
                "CRM Deal", ref_dn, ["contact", "organization"], as_dict=True
            ) or {}
            doc.deal = doc.get("deal") or ref_dn
            doc.contacto = doc.get("contacto") or ref.get("contact")
            doc.organizacion = doc.get("organizacion") or ref.get("organization")
        elif ref_dt == "CRM Lead":
            ref = frappe.db.get_value(
                "CRM Lead", ref_dn, ["contact", "organization"], as_dict=True
            ) or {}
            doc.contacto = doc.get("contacto") or ref.get("contact")
            doc.organizacion = doc.get("organizacion") or _org_por_nombre(
                ref.get("organization")
            )
        elif ref_dt == "Contact":
            doc.contacto = doc.get("contacto") or ref_dn
            doc.organizacion = doc.get("organizacion") or _org_por_nombre(
                frappe.db.get_value("Contact", ref_dn, "company_name")
            )
        elif ref_dt == "CRM Organization":
            doc.organizacion = doc.get("organizacion") or ref_dn
    except Exception as exc:
        frappe.log_error(f"tareas.autollenar: {exc}", "Tareas")


def notificar_asignacion(doc, method=None):
    """doc_event `after_insert` de CRM Task — push al usuario asignado.

    Solo `after_insert`, a propósito: Frappe corre `on_update` también durante el
    propio insert (no solo en ediciones posteriores de un doc ya existente), así que
    registrar esta misma función en ambos eventos la disparaba dos veces en la misma
    alta — y el segundo `.save()` de la request tronaba con `TimestampMismatchError`
    (encontrado validando este cambio, 2026-09-20). Cubre el caso real: una tarea que
    nace con `assigned_to`. La reasignación de una tarea ya existente no reavisa por
    ahora. Nunca lanza: un fallo de push no puede tumbar el guardado de la tarea,
    igual que `autollenar`."""
    asignado = doc.get("assigned_to")
    if not asignado or asignado == frappe.session.user:
        return
    try:
        push.notify_user(
            asignado,
            title="Nueva tarea asignada",
            body=(doc.get("title") or "Sin título")[:120],
            url="/crm/tasks",
            tag=f"tarea-{doc.name}",
        )
    except Exception as exc:
        frappe.log_error(f"tareas.notificar_asignacion: {exc}", "Tareas")


@frappe.whitelist()
def autollenar_existentes(apply: int = 0, limite: int = 0) -> dict:
    """Backfill de las tareas ya creadas (823 al 2026-09-15). Dry-run por defecto."""
    apply = frappe.utils.cint(apply)
    filtros = {"reference_docname": ["is", "set"]}
    tareas = frappe.get_all(
        "CRM Task", filters=filtros,
        fields=["name", "reference_doctype", "reference_docname", "contacto",
                "organizacion", "deal"],
        limit_page_length=frappe.utils.cint(limite) or 0,
    )
    faltantes, cambios = 0, []
    for t in tareas:
        if t.get("contacto") and t.get("organizacion") and t.get("deal"):
            continue
        faltantes += 1
        doc = frappe.get_doc("CRM Task", t["name"])
        autollenar(doc)
        nuevos = {k: doc.get(k) for k in ("contacto", "organizacion", "deal")}
        if (nuevos["contacto"], nuevos["organizacion"], nuevos["deal"]) != (
            t.get("contacto"), t.get("organizacion"), t.get("deal")
        ):
            cambios.append((t["name"], nuevos["contacto"], nuevos["organizacion"], nuevos["deal"]))
            if apply:
                # set_value, no save(): son 800+ tareas y `save()` les movería el
                # `modified` a todas, ensuciando el orden del panel de Tareas.
                frappe.db.set_value("CRM Task", t["name"], nuevos, update_modified=False)
    if apply:
        frappe.db.commit()
    return {"apply": bool(apply), "total": len(tareas), "con_huecos": faltantes,
            "cambiadas": len(cambios), "detalle": cambios[:30]}
