# Copyright (c) 2026, lavendi.mx
# License: MIT
"""
Sincroniza el NOMBRE del `Contact` de Frappe hacia el contacto de Chatwoot.

Por qué existe
    La bandeja de Conversaciones pinta el nombre de **Chatwoot**, no el de
    Frappe: Chatwoot es el sistema de registro de las conversaciones. Al editar
    el nombre en el CRM el cambio no se veía en la bandeja (reportado por
    Alejandro el 2026-09-15; el caso de Edgar Solís, que aparecía como "😎").

    **Frappe es la fuente de verdad del nombre.** Este módulo lo empuja al
    contacto de Chatwoot que corresponde al mismo teléfono.

Identidad por teléfono, no por correo
    El 79% de los contactos migrados no tiene email (medido 2026-09-06), así que
    el match va por los **últimos 10 dígitos** del número — el mismo criterio
    que usa `api/panel.py` y el agente en `agente-ia/lib/opportunities.js`.

Solo empuja el nombre
    No toca teléfono ni correo: esos pueden haber sido corregidos en Chatwoot
    (o venir del perfil de WhatsApp) y no son lo que el equipo edita aquí.
"""

import re

import frappe

from frappe_chatwoot.utils import chatwoot_client as cw


def telefono_corto(telefono: str | None) -> str | None:
    """Últimos 10 dígitos. Absorbe lada de país, +, espacios y guiones."""
    digitos = re.sub(r"\D", "", telefono or "")
    return digitos[-10:] if len(digitos) >= 10 else None


def nombre_de(doc) -> str:
    """Nombre a mostrar: el `full_name` del Contact, o armado de las partes."""
    return (
        doc.get("full_name")
        or " ".join(x for x in [doc.get("first_name"), doc.get("last_name")] if x)
        or ""
    ).strip()


def _contacto_chatwoot(telefono: str | None):
    """Contacto de Chatwoot con ese teléfono, o None. Filtra por últimos 10
    dígitos porque `search_contacts` también devuelve coincidencias parciales."""
    corto = telefono_corto(telefono)
    if not corto:
        return None
    for c in cw.search_contacts(corto):
        if telefono_corto(c.get("phone_number")) == corto:
            return c
    return None


def sincronizar_nombre(doc, method=None):
    """doc_event de `Contact`: empuja el nombre a Chatwoot.

    Nunca lanza: un fallo de Chatwoot no puede tumbar el guardado del contacto
    en el CRM. En `on_update` solo actúa si cambió algo del nombre o el teléfono
    (cada guardado del panel lateral del CRM dispara este hook).
    """
    if frappe.flags.in_import or frappe.flags.in_migrate or frappe.flags.in_patch:
        return
    try:
        if method == "on_update":
            try:
                relevante = any(
                    doc.has_value_changed(f)
                    for f in ("first_name", "last_name", "full_name", "mobile_no", "phone")
                )
            except Exception:
                # Sin `_doc_before_save` no se puede comparar; se sincroniza por
                # si acaso en vez de dejar el nombre viejo.
                relevante = True
            if not relevante:
                return

        nombre = nombre_de(doc)
        telefono = doc.get("mobile_no") or doc.get("phone")
        if not nombre or not telefono:
            return
        cw_contacto = _contacto_chatwoot(telefono)
        if not cw_contacto or (cw_contacto.get("name") or "").strip() == nombre:
            return
        cw.update_contact(cw_contacto["id"], name=nombre)
        frappe.logger("chatwoot_contactos").info(
            f"nombre sincronizado: Contact {doc.get('name')} -> Chatwoot "
            f"{cw_contacto['id']}: {nombre!r} (antes {cw_contacto.get('name')!r})"
        )
    except Exception as exc:
        frappe.log_error(
            f"chatwoot_contactos.sincronizar_nombre: {exc}", "Chatwoot Contactos"
        )


_RE_LETRA = re.compile(r"[0-9A-Za-zÁÉÍÓÚÑáéíóúñ]")


def _es_informativo(nombre: str | None) -> bool:
    """¿El nombre de Chatwoot sirve para mostrar en la bandeja?

    Solo se reemplazan los NO informativos: vacío, puros emojis/símbolos, un
    teléfono, o el genérico "Contacto +52…". Un nombre real (aunque difiera del
    de Frappe en acentos o mayúsculas) se respeta: el match por teléfono puede
    equivocarse y bajar "Estrublock" a un número sería peor que no tocarlo.
    """
    n = (nombre or "").strip()
    if not n or not _RE_LETRA.search(n):
        return False
    digitos = re.sub(r"\D", "", n)
    if len(digitos) >= 7 and len(digitos) >= len(n) - 4:
        return False
    if n.lower().startswith("contacto +"):
        return False
    return True


def _nombre_frappe(corto: str) -> str | None:
    """Nombre del Contact de Frappe para ese teléfono, si es **inequívoco**.

    Si dos contactos distintos comparten los últimos 10 dígitos, no se adivina:
    devolver el primero renombraría al contacto de otra persona."""
    nombres = set()
    for campo in ("mobile_no", "phone"):
        for r in frappe.get_all(
            "Contact", filters=[[campo, "like", f"%{corto}"]],
            fields=["full_name"], limit=5,
        ):
            if r.full_name and r.full_name.strip():
                nombres.add(r.full_name.strip())
    return next(iter(nombres)) if len(nombres) == 1 else None


@frappe.whitelist()
def sincronizar_todos(apply: int = 0, agresivo: int = 0) -> dict:
    """Backfill: recorre los contactos de Chatwoot y les pone el nombre del
    Contact de Frappe del mismo teléfono. Dry-run por defecto.

    Dos modos:
      · Conservador (default): solo corrige nombres de Chatwoot **no informativos**
        (emoji/vacío/teléfono/genérico) con match **inequívoco** en Frappe. El resto
        se arregla solo cuando el equipo edite el contacto (doc_event).
      · `agresivo=1`: reemplaza TAMBIÉN los nombres "reales" de Chatwoot por el del
        Contact de Frappe (pedido de Alejandro 2026-09-15: la fila de la bandeja
        debe decir lo mismo que el contacto — caso Alexis Leon vs "Diseño Aerotec").
        Sigue exigiendo match **inequívoco**: si dos Contact de Frappe comparten los
        últimos 10 dígitos, no adivina. Se guarda el nombre previo en el detalle
        (auditable; Chatwoot no versiona el nombre).

    ⚠ En modo agresivo, un teléfono mal capturado renombra al contacto equivocado.
    Por eso el match debe ser único y el detalle trae el valor anterior."""
    apply = frappe.utils.cint(apply)
    agresivo = frappe.utils.cint(agresivo)

    # Primero se leen TODAS las páginas, luego se escribe. Renombrar cambia el
    # orden de `list_contacts` (ordena por actividad), así que renombrar dentro
    # del loop de páginas hace que la paginación se salte contactos — medido el
    # 2026-09-15: un apply de 18 cambios solo aplicó 15.
    todos, page = [], 1
    while True:
        filas = cw.list_contacts(page=page).get("payload") or []
        if not filas:
            break
        todos.extend(filas)
        if len(filas) < 15:
            break
        page += 1

    cambios, sin_match, ambiguo, informativo, igual = [], 0, 0, 0, 0
    vistos = 0
    for c in todos:
        vistos += 1
        cw_nombre = c.get("name")
        if not agresivo and _es_informativo(cw_nombre):
            informativo += 1
            continue
        corto = telefono_corto(c.get("phone_number"))
        if not corto:
            sin_match += 1
            continue
        nombre_frappe = _nombre_frappe(corto)
        if not nombre_frappe:
            ambiguo += 1
            continue
        if _es_informativo(nombre_frappe) is False:
            # El nombre de Frappe tampoco sirve (es un teléfono, etc.).
            ambiguo += 1
            continue
        if (cw_nombre or "").strip() == nombre_frappe:
            igual += 1
            continue
        cambios.append((c.get("id"), cw_nombre, nombre_frappe))
        if apply:
            cw.update_contact(c["id"], name=nombre_frappe)
    return {
        "apply": bool(apply),
        "agresivo": bool(agresivo),
        "vistos": vistos,
        "iguales": igual,
        "informativos": informativo,
        "sin_match_o_ambiguo": sin_match + ambiguo,
        "cambios": len(cambios),
        "detalle": cambios[:60],
    }
