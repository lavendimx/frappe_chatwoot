# Copyright (c) 2026, lavendi.mx
# License: MIT
"""
Buscador global — pantalla "Buscar" del CRM (lavendi.mx, no es upstream de Frappe).

Un solo cuadro de texto que busca en paralelo: Contactos, Oportunidades (Deal +
Lead), Organizaciones y conversaciones de Chatwoot. Reemplaza tener que adivinar
en qué sección vive un dato — el equipo hoy busca por nombre, teléfono o correo
sin saber de antemano si esa persona es un contacto, una oportunidad abierta o
solo tiene un hilo de WhatsApp.

Igual que en panel.py, la identidad real es el TELÉFONO (79% de los contactos
migrados de GHL no tiene correo) — si el texto tiene pinta de número, se busca
por los últimos 10 dígitos; si no, por coincidencia de texto en nombre/empresa.
"""

import re

import frappe

from frappe_chatwoot.utils import chatwoot_client as cw

from .chatwoot import validate_role

LIMIT_POR_TIPO = 8


def _es_telefono(q: str) -> str | None:
    digitos = re.sub(r"\D", "", q)
    return digitos if len(digitos) >= 4 else None


def _dedup(filas: list[dict]) -> list[dict]:
    vistos = set()
    unicas = []
    for f in filas:
        if f["name"] in vistos:
            continue
        vistos.add(f["name"])
        unicas.append(f)
    return unicas


def _buscar_contactos(q: str, digitos: str | None) -> list[dict]:
    if digitos:
        patron = f"%{digitos[-10:] if len(digitos) >= 10 else digitos}"
        filtros = [["mobile_no", "like", patron]]
    else:
        filtros = [["name", "like", f"%{q}%"]]
    filas = frappe.get_all(
        "Contact",
        filters=filtros,
        fields=["name", "first_name", "last_name", "company_name", "mobile_no", "email_id"],
        order_by="modified desc",
        limit=LIMIT_POR_TIPO,
    )
    for f in filas:
        f["label"] = " ".join(filter(None, [f.get("first_name"), f.get("last_name")])) or f["name"]
        f["subtitulo"] = f.get("mobile_no") or f.get("email_id") or ""
    return filas


def _buscar_oportunidades(q: str, digitos: str | None, contactos: list[dict]) -> list[dict]:
    contact_names = [c["name"] for c in contactos]
    condiciones = []
    valores: list[str] = []
    if not digitos:
        condiciones.append("(organization LIKE %s OR name LIKE %s)")
        valores.extend([f"%{q}%", f"%{q}%"])
    if contact_names:
        placeholders = ", ".join(["%s"] * len(contact_names))
        condiciones.append(f"contact IN ({placeholders})")
        valores.extend(contact_names)
    if not condiciones:
        return []

    deals = frappe.db.sql(
        f"""
        SELECT name, organization, status, deal_value, currency, contact, modified
        FROM `tabCRM Deal`
        WHERE {" OR ".join(condiciones)}
        ORDER BY modified DESC
        LIMIT {LIMIT_POR_TIPO}
        """,
        valores,
        as_dict=True,
    )
    leads = frappe.db.sql(
        f"""
        SELECT name, organization, status, contact, modified
        FROM `tabCRM Lead`
        WHERE {" OR ".join(condiciones)}
        ORDER BY modified DESC
        LIMIT {LIMIT_POR_TIPO}
        """,
        valores,
        as_dict=True,
    )
    for d in deals:
        d["tipo"] = "Deal"
        d["label"] = d.get("organization") or d["name"]
        d["subtitulo"] = d.get("status") or ""
    for l in leads:
        l["tipo"] = "Lead"
        l["label"] = l.get("organization") or l["name"]
        l["subtitulo"] = l.get("status") or ""
    return _dedup(deals + leads)[:LIMIT_POR_TIPO]


def _buscar_organizaciones(q: str, digitos: str | None) -> list[dict]:
    if digitos:
        return []  # las organizaciones no tienen teléfono propio en el doctype
    filas = frappe.get_all(
        "CRM Organization",
        filters=[["organization_name", "like", f"%{q}%"]],
        fields=["name", "organization_name", "website", "industry"],
        order_by="modified desc",
        limit=LIMIT_POR_TIPO,
    )
    for f in filas:
        f["label"] = f.get("organization_name") or f["name"]
        f["subtitulo"] = f.get("website") or f.get("industry") or ""
    return filas


def _buscar_conversaciones(q: str) -> list[dict]:
    """Vía Chatwoot: busca contactos ahí (mismo motor que search_crm_contacts de
    panel.py) y trae sus conversaciones. Si Chatwoot no responde, se omite esta
    sección — un buscador global no debe caerse completo por una dependencia."""
    try:
        cw_contactos = cw.search_contacts(q)[:5]
    except Exception as exc:
        frappe.log_error(title="buscar_global: Chatwoot no responde", message=str(exc))
        return []

    resultado = []
    for contacto in cw_contactos:
        try:
            convs = cw.get_conversations_for_contact(contacto.get("id"))
        except Exception:
            continue
        for conv in convs[:3]:
            resultado.append(
                {
                    "name": str(conv.get("id")),
                    "label": contacto.get("name") or contacto.get("phone_number") or "Sin nombre",
                    "subtitulo": (conv.get("meta") or {}).get("channel") or contacto.get("phone_number") or "",
                    "conversation_id": conv.get("id"),
                }
            )
    return resultado[:LIMIT_POR_TIPO]


@frappe.whitelist()
def buscar_global(q: str = "") -> dict:
    """Un query, cuatro fuentes en paralelo (secuencial en código, pero cada una
    es una consulta puntual — no un barrido de toda la tabla). Umbral de 2
    caracteres: con 1 letra cualquier LIKE devuelve cientos de filas y el
    buscador se vuelve inútil (y lento)."""
    validate_role()
    q = (q or "").strip()
    if len(q) < 2:
        return {"contactos": [], "oportunidades": [], "organizaciones": [], "conversaciones": []}

    digitos = _es_telefono(q)
    contactos = _buscar_contactos(q, digitos)
    return {
        "contactos": contactos,
        "oportunidades": _buscar_oportunidades(q, digitos, contactos),
        "organizaciones": _buscar_organizaciones(q, digitos),
        "conversaciones": _buscar_conversaciones(q),
    }
