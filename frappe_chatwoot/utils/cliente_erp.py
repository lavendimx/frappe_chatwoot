# Copyright (c) 2026, lavendi.mx
# License: MIT
"""
Vínculo `Contact` (CRM) <-> `Customer` (ERPNext) y alta automática del Customer.

Por qué existe el alta automática (2026-09-25)
    Ningún alta manual desde el CRM creaba `Customer` — solo lo hizo la migración
    masiva de GHL (2026-09-10, `patches/vincular_customer_contacto.py`, 99
    Customer). Sin `Customer`, el contacto queda invisible en 3 lugares que ya
    dependen de ese doctype:
      - `api/facturacion.py::buscar_clientes` — selector de "Nueva nota de cobro".
      - `api/facturacion.py::cartera_de_contacto` — cartera en el panel lateral.
      - `utils/autofollowup.py::es_cliente_activo` — detección de cliente activo.
    Caso real que lo destapó: **Alma Cabello** (Casas Riscos), dada de alta a
    mano el 2026-09-25, invisible en el selector de cobro.

EL VÍNCULO ES `Dynamic Link`, NO `ghl_contact_id` (2026-10-18)
    Hasta hoy el puente era `Contact.ghl_contact_id == Customer.ghl_contact_id`:
    un campo de texto heredado de la migración de GoHighLevel. Dos problemas
    medidos el 2026-10-18:

      1. **Se desincroniza en silencio.** Fusionar dos `Contact` (la herramienta
         de Contactos) consolida `tabContact` pero no mira `tabCustomer`: el
         Customer del contacto absorbido sobrevive activo y huérfano, y el
         `on_update` del Contact base crea uno nuevo. Resultado real: "Eleazer
         Moreno" aparecía DOS veces en el selector de Cobranza con el contacto
         ya fusionado. Caso que destapó esto.
      2. **GHL ya no existe** (retirado 2026-09-11). El campo dejó de ser un id
         de GHL: a los contactos nuevos se les inventaba un `manual-<hash>`
         sintético para poder colgarles el vínculo. Un UUID interno con nombre
         de un sistema muerto.

    Ahora el puente es el mecanismo **nativo de Frappe/ERPNext**: la child table
    `Dynamic Link` del `Contact` (`link_doctype="Customer"`), más
    `Customer.customer_primary_contact` en el lado inverso. Es el mismo que usa
    ERPNext de fábrica para ligar contactos a clientes, y lo conocen tanto el
    Desk como `rename_doc(..., merge=True)`.

    `Contact.ghl_contact_id` **se conserva** — pero solo como procedencia
    histórica de los 4,221 contactos que sí vinieron de GHL. Ya no se escribe,
    ya no se consulta para resolver clientes, y los valores sintéticos
    `manual-%` se limpian en `patches/vinculo_contact_customer_nativo.py`.

Compatibilidad durante la transición
    `customer_de_contacto` hace *lazy backfill*: si no encuentra `Dynamic Link`
    pero sí un match por el `ghl_contact_id` viejo, crea el link en ese momento
    y devuelve el Customer. Así el código nuevo funciona desde el primer request
    aunque el patch de backfill todavía no haya corrido, y el patch solo acelera
    lo que de todos modos pasaría solo.

Nombre del Customer: el de la PERSONA, no el de la empresa
    Verificado contra los 99 migrados: `customer_name` sigue siempre el nombre
    de la persona (`full_name`), incluso cuando el contacto tiene
    `company_name` distinto (ej. Rafael Cardeño / empresa "Medicare México" →
    Customer "Rafael Cardeño Oficial"). Se replica ese criterio: `company_name`
    solo se usa como último recurso, si el contacto no tiene nombre de persona.

Un Customer por Contact, nunca por empresa
    Los 99 migrados no agrupan por empresa — dos contactos de la misma empresa
    tienen dos Customer distintos. Mismo criterio aquí.

Campos default: los que ya usan 98 de los 99 migrados
    `customer_type="Individual"`, `customer_group="Comercial"`,
    `territory="All Territories"` (medido 2026-09-25).
"""

import frappe


# ---------------------------------------------------------------------------
# API de vínculo — único punto por el que se resuelve Contact <-> Customer
# ---------------------------------------------------------------------------

def _erpnext_instalado() -> bool:
    """Sin `erpnext` no existe el doctype `Customer`: los 9 sitios `sofia_plan:
    lite` corren CRM sin facturación. Todo en este módulo es no-op ahí."""
    return "erpnext" in frappe.get_installed_apps()


def customer_de_contacto(contact: str, incluir_deshabilitados: bool = False) -> str | None:
    """`Customer` vinculado a este `Contact`, o `None`.

    Prefiere siempre el Customer **activo**: tras una fusión el absorbido queda
    `disabled=1` pero su `Dynamic Link` puede seguir colgando del contacto
    sobreviviente (a propósito — es la trazabilidad de que esa factura vieja
    era de esta misma persona).
    """
    if not contact or not _erpnext_instalado():
        return None

    filas = frappe.get_all(
        "Dynamic Link",
        filters={"parenttype": "Contact", "parent": contact, "link_doctype": "Customer"},
        pluck="link_name",
    )
    if filas:
        if incluir_deshabilitados:
            return filas[0]
        activos = [
            c for c in filas
            if frappe.db.get_value("Customer", c, "disabled") in (0, None)
        ]
        if activos:
            return activos[0]
        return None

    return _backfill_desde_campo_legacy(contact)


def _backfill_desde_campo_legacy(contact: str) -> str | None:
    """Puente de transición: resuelve por el `ghl_contact_id` viejo y deja el
    `Dynamic Link` creado para que la siguiente consulta ya no pase por aquí.

    Se borra cuando `patches/vinculo_contact_customer_nativo.py` haya corrido en
    los 2 sitios con ERPNext y el verificador reporte 0 Customers sin link.
    """
    ghl_id = (frappe.db.get_value("Contact", contact, "ghl_contact_id") or "").strip()
    if not ghl_id:
        return None
    customer = frappe.db.get_value(
        "Customer", {"ghl_contact_id": ghl_id, "disabled": 0}, "name"
    )
    if not customer:
        return None
    try:
        vincular(contact, customer)
    except Exception as exc:  # nunca romper una lectura por el backfill
        frappe.log_error(f"cliente_erp._backfill_desde_campo_legacy: {exc}", "Cliente ERP")
    return customer


def contacto_de_customer(customer: str) -> str | None:
    """`Contact` dueño de este `Customer`, o `None`. Inverso de
    `customer_de_contacto`; lo usan la nota de cobro (destinatario) y la
    consolidación de la fusión."""
    if not customer or not _erpnext_instalado():
        return None

    contact = frappe.db.get_value(
        "Dynamic Link",
        {"parenttype": "Contact", "link_doctype": "Customer", "link_name": customer},
        "parent",
    )
    if contact:
        return contact

    primario = frappe.db.get_value("Customer", customer, "customer_primary_contact")
    if primario and frappe.db.exists("Contact", primario):
        return primario

    ghl_id = (frappe.db.get_value("Customer", customer, "ghl_contact_id") or "").strip()
    if ghl_id:
        contact = frappe.db.get_value("Contact", {"ghl_contact_id": ghl_id}, "name")
        if contact:
            try:
                vincular(contact, customer)
            except Exception:
                pass
            return contact
    return None


def vincular(contact: str, customer: str) -> None:
    """Idempotente: deja el `Dynamic Link` Contact→Customer y el
    `customer_primary_contact` del lado de ERPNext.

    `customer_primary_contact` importa de verdad: es el campo que ERPNext usa
    para decidir a quién se le manda la factura. Dejarlo vacío (como estaba en
    los 173 Customers, 0 con primary contact) obliga a cada consumidor a
    re-resolver el correo por su cuenta.
    """
    if not (contact and customer) or not _erpnext_instalado():
        return

    ya = frappe.db.exists(
        "Dynamic Link",
        {
            "parenttype": "Contact",
            "parent": contact,
            "link_doctype": "Customer",
            "link_name": customer,
        },
    )
    if not ya:
        doc = frappe.get_doc("Contact", contact)
        doc.append("links", {"link_doctype": "Customer", "link_name": customer})
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        # `ignore_version`: un link técnico no merece una entrada en el
        # historial del contacto que el vendedor lee.
        doc.flags.ignore_version = True
        doc.save()

    actual = frappe.db.get_value("Customer", customer, "customer_primary_contact")
    if not actual:
        frappe.db.set_value(
            "Customer", customer, "customer_primary_contact", contact,
            update_modified=False,
        )


def desvincular(contact: str, customer: str) -> None:
    """Quita el `Dynamic Link` Contact→Customer. Solo lo usa la consolidación de
    la fusión cuando reasigna un Customer a otro contacto."""
    if not (contact and customer) or not _erpnext_instalado():
        return
    frappe.db.delete(
        "Dynamic Link",
        {
            "parenttype": "Contact",
            "parent": contact,
            "link_doctype": "Customer",
            "link_name": customer,
        },
    )
    if frappe.db.get_value("Customer", customer, "customer_primary_contact") == contact:
        frappe.db.set_value(
            "Customer", customer, "customer_primary_contact", None, update_modified=False
        )


# ---------------------------------------------------------------------------
# Alta automática del Customer al guardar un Contact
# ---------------------------------------------------------------------------

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
    tiene `Customer` vinculado, se lo crea.

    Nunca lanza: un fallo aquí no puede tumbar el guardado del Contact — solo
    queda en Error Log. Se salta en imports/migraciones masivas para no crear
    4,000+ Customer de golpe ni interferir con los patches.

    `in_fusion_contactos`: durante una fusión el contacto base se guarda varias
    veces (copia de campos, reasignación de referencias) ANTES de que la
    consolidación de Customers corra. Sin este guard, ese `on_update` crea un
    Customer nuevo para el contacto base y la fusión termina con dos —
    exactamente el bug de "Eleazer Moreno" del 2026-10-17 08:35:22, un segundo
    antes de que se borraran los contactos absorbidos. La fusión llama a
    `consolidar_customers_de_fusion` al final, que es quien decide cuál queda.
    """
    if frappe.flags.in_import or frappe.flags.in_migrate or frappe.flags.in_patch:
        return
    if getattr(frappe.flags, "in_fusion_contactos", False):
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
    (sin nombre utilizable, o sin ERPNext en el sitio).
    """
    if not _erpnext_instalado():
        return None

    contact = doc.get("name")
    existente = customer_de_contacto(contact)
    if existente:
        return frappe.get_doc("Customer", existente)

    nombre = _nombre_customer(doc)
    if not nombre:
        frappe.logger("cliente_erp").info(
            f"Contact {contact} sin nombre utilizable — no se crea Customer"
        )
        return None

    customer = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": nombre,
        "customer_type": "Individual",
        "customer_group": "Comercial",
        "territory": "All Territories",
    })
    customer.insert(ignore_permissions=True)
    vincular(contact, customer.name)
    frappe.logger("cliente_erp").info(
        f"Customer creado automáticamente: {customer.name} (Contact {contact})"
    )
    return customer
