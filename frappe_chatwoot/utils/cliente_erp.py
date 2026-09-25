# Copyright (c) 2026, lavendi.mx
# License: MIT
"""
Crea automáticamente el `Customer` de ERPNext al dar de alta un `Contact` (2026-09-25).

Por qué existe
    Ningún alta manual desde el CRM crea `Customer` — solo lo hizo la migración
    masiva de GHL (2026-09-10, `patches/vincular_customer_contacto.py`, 99
    Customer). Sin `Customer`, el contacto queda invisible en 3 lugares que ya
    dependen de ese doctype:
      - `api/facturacion.py::buscar_clientes` — selector de "Nueva nota de cobro".
      - `api/facturacion.py::cartera_de_contacto` — cartera en el panel lateral.
      - `utils/autofollowup.py::es_cliente_activo` — detección de cliente activo.
    Caso real que lo destapó: **Alma Cabello** (Casas Riscos), dada de alta a
    mano el 2026-09-25, invisible en el selector de cobro.

El vínculo es `Contact.ghl_contact_id` == `Customer.ghl_contact_id`
    Verificado contra los 99 Customer migrados: no hay `Dynamic Link` ni uso de
    `customer_primary_contact` (0 de 99) — el único puente real es ese campo,
    a propósito: es inerte para ERPNext (no toca facturación/contabilidad),
    mientras que `customer_primary_contact` sí decide a quién se le manda la
    factura (ver docstring de `vincular_customer_contacto.py`).
    Un contacto migrado trae un id real de GHL; un alta manual no tiene uno —
    se genera un id sintético con prefijo `manual-` (deja rastro de que no
    viene de GHL) y se escribe en AMBOS doctypes para que el vínculo sea
    bidireccional.

Nombre del Customer: el de la PERSONA, no el de la empresa
    Verificado contra los 99 migrados: `customer_name` sigue siempre el nombre
    de la persona (`full_name`), incluso cuando el contacto tiene
    `company_name` distinto (ej. Rafael Cardeño / empresa "Medicare México" →
    Customer "Rafael Cardeño Oficial"). Se replica ese criterio: `company_name`
    solo se usa como último recurso, si el contacto no tiene nombre de persona.

Un Customer por Contact, nunca por empresa
    Los 99 migrados no agrupan por empresa — dos contactos de la misma empresa
    tienen dos Customer distintos. Mismo criterio aquí: nunca se busca un
    Customer existente por nombre de empresa, solo por el `ghl_contact_id` de
    ESE contacto puntual.

Campos default: los que ya usan 98 de los 99 migrados
    `customer_type="Individual"`, `customer_group="Comercial"`,
    `territory="All Territories"` (medido 2026-09-25).
"""

import frappe


def _nombre_customer(doc) -> str:
    """Nombre a usar como `customer_name`: el de la persona, o la empresa
    como último recurso. Mismo ensamblado que `chatwoot_contactos.nombre_de`."""
    nombre = (
        doc.get("full_name")
        or " ".join(x for x in [doc.get("first_name"), doc.get("last_name")] if x)
        or ""
    ).strip()
    return nombre or (doc.get("company_name") or "").strip()


def crear_customer_si_falta(doc, method=None):
    """doc_event de `Contact` (`after_insert`/`on_update`): si el contacto no
    tiene `Customer` vinculado por `ghl_contact_id`, se lo crea.

    Nunca lanza: un fallo aquí no puede tumbar el guardado del Contact — solo
    queda en Error Log. Se salta en imports/migraciones masivas para no crear
    99+ Customer de golpe ni interferir con `vincular_customer_contacto.py` u
    otras migraciones futuras (mismo guard que `chatwoot_contactos.sincronizar_nombre`).
    """
    if frappe.flags.in_import or frappe.flags.in_migrate or frappe.flags.in_patch:
        return
    try:
        _asegurar_customer(doc)
    except Exception as exc:
        frappe.log_error(f"cliente_erp.crear_customer_si_falta: {exc}", "Cliente ERP")


def _asegurar_customer(doc) -> "frappe.model.document.Document | None":
    """Lógica idempotente, sin try/except propio (lo pone el caller) y sin
    `frappe.db.commit()` — corre dentro del ciclo de vida del request que
    guarda el Contact; si algo posterior en esa misma request truena, el
    rollback debe incluir también este Customer (atomicidad con el alta).

    Devuelve el Customer (nuevo o ya existente) o `None` si no se pudo crear
    (sin nombre utilizable). Público dentro del módulo para que el fix puntual
    y las pruebas reusen exactamente esta misma ruta.
    """
    ghl_id = (doc.get("ghl_contact_id") or "").strip()
    if ghl_id:
        existente = frappe.db.get_value("Customer", {"ghl_contact_id": ghl_id}, "name")
        if existente:
            return frappe.get_doc("Customer", existente)

    nombre = _nombre_customer(doc)
    if not nombre:
        frappe.logger("cliente_erp").info(
            f"Contact {doc.get('name')} sin nombre utilizable — no se crea Customer"
        )
        return None

    if not ghl_id:
        ghl_id = f"manual-{frappe.generate_hash(length=16)}"
        # Vínculo bidireccional: sin escribirlo también en el Contact,
        # `cartera_de_contacto`/`autofollowup` (que leen `Contact.ghl_contact_id`)
        # no encontrarían el Customer que se crea a continuación.
        frappe.db.set_value(
            "Contact", doc.name, "ghl_contact_id", ghl_id, update_modified=False
        )
        doc.ghl_contact_id = ghl_id  # por si el caller sigue usando el doc en memoria

    # Re-chequeo tras generar el id: dos saves casi simultáneos del mismo
    # Contact sin ghl_contact_id no deben producir dos Customer.
    existente = frappe.db.get_value("Customer", {"ghl_contact_id": ghl_id}, "name")
    if existente:
        return frappe.get_doc("Customer", existente)

    customer = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": nombre,
        "customer_type": "Individual",
        "customer_group": "Comercial",
        "territory": "All Territories",
        "ghl_contact_id": ghl_id,
    })
    customer.insert(ignore_permissions=True)
    frappe.logger("cliente_erp").info(
        f"Customer creado automáticamente: {customer.name} "
        f"(Contact {doc.get('name')}, ghl_contact_id={ghl_id})"
    )
    return customer
