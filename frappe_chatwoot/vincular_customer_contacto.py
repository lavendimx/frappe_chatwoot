"""Vincula cada Customer de ERPNext con su Contact del CRM (2026-09-14). Idempotente.

    bench --site crm.lavendi.mx execute frappe_chatwoot.vincular_customer_contacto.ejecutar
    bench --site crm.lavendi.mx execute frappe_chatwoot.vincular_customer_contacto.ejecutar --kwargs "{'apply':1}"

POR QUÉ EXISTE
    `migracion-erp/cargar_cartera.py` creó un Customer por `contact_id` de GHL
    pero **solo guardó el nombre** — no dejó el id. Hoy el único puente entre
    la ficha del CRM y la cartera es coincidencia de nombre, y ya hay
    colisiones resueltas con sufijo (`"Nombre (2)"`), así que emparejar por
    nombre le colgaría los adeudos de un cliente a otro.

    Eso bloquea la sección de facturación en el panel de Conversaciones: es
    una pantalla que el equipo consulta **mientras habla con el cliente**;
    mostrarle ahí el saldo equivocado es peor que no mostrar nada.

POR QUÉ UN CAMPO PROPIO Y NO `customer_primary_contact`
    Ese campo nativo lo usa ERPNext para resolver el contacto de facturación
    (direcciones, correos de la factura). Escribirlo masivamente cambiaría a
    quién se le envían documentos fiscales — efecto colateral en contabilidad
    que este vínculo no necesita. `ghl_contact_id` es inerte: solo sirve para
    encontrar, no altera ningún flujo existente.

DE DÓNDE SALE EL MAPA
    Se reconstruye desde `migracion-erp/cartera_ghl.json` replicando literalmente
    `_clave_cliente()` y `_nombre_cliente()` del cargador — misma entrada, mismo
    algoritmo, mismo resultado. No es una heurística nueva: es la función que
    ya decidió cómo se llama cada Customer, corrida al revés.

    Medido antes de escribir: 97 clientes únicos, **97 con `contact_id` real**,
    0 que dependan del fallback por nombre.

NO TOCA los Customers que no aparecen en la cartera migrada (altas nuevas
posteriores). Esos se vinculan solos al crearse desde el CRM, o a mano.
"""

import json
import os

import frappe

CAMPO = {
    "fieldname": "ghl_contact_id",
    "fieldtype": "Data",
    "label": "ID de contacto (origen)",
    "description": "Vínculo con el Contact del CRM. Sembrado desde la migración de GHL; "
                   "no lo edites a mano.",
    "read_only": 1,
    "insert_after": "customer_name",
}

# El JSON vive fuera del contenedor; se copia junto a este archivo al desplegar.
RUTA_MAPA = os.path.join(os.path.dirname(__file__), "mapa_customer_ghl.json")


def _asegurar_campo(apply: bool) -> str:
    if frappe.db.exists("Custom Field", {"dt": "Customer", "fieldname": CAMPO["fieldname"]}):
        return "ya existía"
    if not apply:
        return "se crearía"
    frappe.get_doc({"doctype": "Custom Field", "dt": "Customer", **CAMPO}).insert(
        ignore_permissions=True
    )
    # Índice: la consulta del panel entra por este campo en cada apertura de hilo.
    frappe.db.add_index("Customer", ["ghl_contact_id"])
    return "creado"


def ejecutar(apply=0):
    apply = bool(frappe.utils.cint(apply))
    estado_campo = _asegurar_campo(apply)
    print(f"CAMPO Customer.ghl_contact_id: {estado_campo}")

    if estado_campo == "se crearía":
        print("DRY-RUN: sin el campo no se puede medir la siembra. "
              "Corre con apply=1 para crear campo y sembrar en una sola pasada.")
        return

    with open(RUTA_MAPA, encoding="utf-8") as fh:
        mapa = json.load(fh)  # {contact_id: customer_name}

    escritos = ya = sin_customer = sin_contacto = 0
    faltantes = []
    for contact_id, customer in mapa.items():
        if not frappe.db.exists("Customer", customer):
            sin_customer += 1
            faltantes.append(customer)
            continue
        actual = frappe.db.get_value("Customer", customer, "ghl_contact_id")
        if actual == contact_id:
            ya += 1
            continue
        # Solo se siembra si el Contact existe: un id que no resuelve a nada
        # daría un panel en blanco sin explicación, peor que no tener vínculo.
        if not frappe.db.exists("Contact", {"ghl_contact_id": contact_id}):
            sin_contacto += 1
            continue
        if apply:
            frappe.db.set_value("Customer", customer, "ghl_contact_id", contact_id,
                                update_modified=False)
        escritos += 1

    if apply:
        frappe.db.commit()
    print(json.dumps({
        "apply": apply,
        "en_mapa": len(mapa),
        "vinculados" if apply else "a_vincular": escritos,
        "ya_estaban": ya,
        "customer_inexistente": sin_customer,
        "contacto_inexistente": sin_contacto,
        "faltantes": faltantes[:10],
    }, ensure_ascii=False, indent=2))
