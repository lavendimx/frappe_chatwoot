# Copyright (c) 2026, lavendi.mx
# License: MIT
"""
Extensiones de lavendi.mx sobre la bandeja de Conversaciones — no son upstream.

Dos cosas que el equipo tenía en GoHighLevel y aquí faltaban:

1. `get_contact_panel` — el panel lateral del contacto: quién es, sus
   oportunidades y sus notas, sin salir del hilo. En GHL esto era la columna
   derecha de la conversación; sin ella hay que abrir el CRM en otra pestaña y
   buscar a mano, que es justo lo que hace que nadie mire el contexto antes de
   contestar.

2. `search_crm_contacts` / `start_conversation` — abrir una conversación nueva
   desde el CRM. Hasta ahora Sofía solo podía CONTESTAR: si el equipo quería
   escribirle primero a alguien, tenía que hacerlo desde el celular, y ese
   mensaje entraba al hilo sin que nadie lo viera en la bandeja.

Identidad del contacto: el teléfono, no el correo. El 79% de los contactos
migrados de GHL no tiene email (medido 2026-09-06), así que todo el match va por
los últimos 10 dígitos del número — mismo criterio que usa el agente en
`agente-ia/lib/opportunities.js`.
"""

import json
import os
import re
import urllib.error
import urllib.request

import frappe

from frappe_chatwoot.utils import autoria
from frappe_chatwoot.utils import chatwoot_client as cw
from frappe_chatwoot.utils import telefono as tel
from frappe_chatwoot.utils.chatwoot_contactos import _contacto_chatwoot

from .chatwoot import _inboxes_del_sitio, _pause_conversation, validate_role

# Cuántas notas/oportunidades devuelve el panel. Es una vista de contexto, no un
# reporte: si alguien necesita el histórico completo abre el registro en el CRM.
MAX_DEALS = 20
MAX_NOTES = 20

# Espejo de `api/chatwoot.MAX_ADJUNTOS`: mismo tope para el primer mensaje.
MAX_ADJUNTOS = 5


@frappe.whitelist()
def sitios_agencia() -> list[dict]:
    """Sitios de cliente para el switcher "Cambiar de cliente" del `UserDropdown.vue`.

    Antes esto era un arreglo a mano (`SITIOS_AGENCIA`) en el propio componente — cada alta
    de cliente requería editar el frontend y recompilar, y por eso un cliente nuevo (caso
    Feramor, 2026-10-15) no aparecía hasta que alguien se acordaba de ese paso suelto.

    Esta función lee los `site_config.json` reales del bench (mismo filesystem para todos
    los sitios, un solo proceso de gunicorn los sirve) en vez de una lista fija: un sitio
    nuevo aparece solo en cuanto su `site_config.json` trae `agencia_label` — sin rebuild del
    frontend. Sitios sin ese campo (scratch, pruebas) quedan fuera a propósito, no por omisión.
    """
    validate_role()
    bench_sites_dir = os.path.join(frappe.utils.get_bench_path(), "sites")
    resultado = []
    for nombre in sorted(os.listdir(bench_sites_dir)):
        ruta_sitio = os.path.join(bench_sites_dir, nombre)
        ruta_config = os.path.join(ruta_sitio, "site_config.json")
        # `sofiav2.lavendi.mx` es symlink a `crm.lavendi.mx` (alias de dominio, no un
        # sitio real) — sin este filtro aparece duplicado ("lavendi.mx" dos veces).
        if os.path.islink(ruta_sitio):
            continue
        if not (nombre.endswith(".lavendi.mx") and os.path.isfile(ruta_config)):
            continue
        try:
            with open(ruta_config) as f:
                config = json.load(f)
        except (OSError, ValueError):
            continue
        label = config.get("agencia_label")
        if not label:
            continue
        url = config.get("agencia_url") or f"https://{nombre}/crm"
        resultado.append({"label": label, "url": url})
    return resultado


def telefono_corto(telefono: str | None) -> str | None:
    """Últimos 10 dígitos. Absorbe lada de país, +, espacios y guiones: el mismo
    número llega como '+52 1 555 966 8622', '5215559668622' o '5559668622'
    según venga de WhatsApp, de la migración de GHL o capturado a mano.
    Delega en `utils.telefono.corto` para no repetir el criterio (2026-09-23)."""
    return tel.corto(telefono)


def _contacto_por_telefono(telefono: str | None) -> str | None:
    """Nombre del `Contact` de Frappe que corresponde a ese número, si existe.

    El LIKE con comodín inicial no usa índice, pero son consultas puntuales por
    conversación abierta, no un barrido de los 4,224 contactos."""
    corto = telefono_corto(telefono)
    if not corto:
        return None
    patron = f"%{corto}"
    for campo in ("mobile_no", "phone"):
        encontrados = frappe.get_all(
            "Contact",
            filters=[[campo, "like", patron]],
            fields=["name"],
            limit=1,
        )
        if encontrados:
            return encontrados[0].name
    return None


def _notas_de(referencias: list[tuple[str, str]]) -> list[dict]:
    """Notas de todos los registros del contacto (su ficha + sus oportunidades).

    En GHL la nota colgaba del contacto; aquí `FCRM Note` es un dynamic link que
    puede apuntar a Contact, CRM Deal o CRM Lead — el equipo las escribe donde
    esté trabajando, así que el panel las junta todas en una sola lista."""
    if not referencias:
        return []
    condiciones = []
    valores: list[str] = []
    for doctype, docname in referencias:
        condiciones.append("(reference_doctype = %s AND reference_docname = %s)")
        valores.extend([doctype, docname])
    filas = frappe.db.sql(
        f"""
        SELECT name, title, content, reference_doctype, reference_docname, owner, modified,
               ghl_note_id
        FROM `tabFCRM Note`
        WHERE {" OR ".join(condiciones)}
        ORDER BY modified DESC
        LIMIT {MAX_NOTES}
        """,
        valores,
        as_dict=True,
    )
    for f in filas:
        # Contenido y título son Text Editor (HTML). El panel los pinta como texto
        # plano: es una vista de lectura rápida, no un editor.
        f["content"] = _texto_plano(f.get("content"))
        titulo = _texto_plano(f.get("title"))
        # Las notas migradas de GHL traen como título el propio arranque del
        # contenido ("[GHL] --------- Summary --------- Denise Mariscal..."), así
        # que mostrarlo duplica el texto en pantalla sin aportar nada.
        if titulo.startswith("[GHL]") or (titulo and f["content"].startswith(titulo[:40])):
            titulo = ""
        f["title"] = titulo
        f["origen_ghl"] = bool(f.pop("ghl_note_id", None))
    return filas


def _texto_plano(html: str | None) -> str:
    limpio = re.sub(r"<[^>]+>", " ", html or "")
    limpio = limpio.replace("&nbsp;", " ").replace("&amp;", "&")
    # Las notas de GHL abren con una regla de guiones que en texto plano es ruido.
    limpio = re.sub(r"-{4,}", " ", limpio)
    return re.sub(r"\s+", " ", limpio).strip()


@frappe.whitelist()
def get_contact_panel(conversation_id: int = None, phone: str = None) -> dict:
    """Contexto de CRM de quien escribe: ficha, oportunidades y notas.

    Se puede llamar con `conversation_id` (la bandeja) o directo con `phone`.
    Degrada suave: si el contacto no está en el CRM devuelve `found: False` en
    vez de reventar — un número desconocido escribiendo por primera vez es un
    caso normal, no un error.
    """
    validate_role()

    telefono = phone
    contacto_chatwoot = {}
    if not telefono and conversation_id:
        conv = cw.get_conversation(frappe.utils.cint(conversation_id))
        sender = ((conv or {}).get("meta") or {}).get("sender") or {}
        contacto_chatwoot = {
            "name": sender.get("name"),
            "phone": sender.get("phone_number"),
            "email": sender.get("email"),
            "identifier": sender.get("identifier"),
        }
        telefono = sender.get("phone_number") or sender.get("identifier")

    contact_name = _contacto_por_telefono(telefono)
    if not contact_name:
        return {"found": False, "chatwoot": contacto_chatwoot, "phone": telefono}

    contacto = frappe.db.get_value(
        "Contact",
        contact_name,
        ["name", "first_name", "last_name", "company_name", "mobile_no", "phone", "email_id", "designation"],
        as_dict=True,
    ) or {}

    deals = frappe.get_all(
        "CRM Deal",
        filters={"contact": contact_name},
        fields=[
            "name", "organization", "status", "deal_value", "currency",
            "deal_owner", "ghl_stage", "ghl_status", "modified",
            # Denormalizados por el motor de secuencias (ver
            # utils/secuencias.py, `_reflejar_en_deal`): el panel muestra si el
            # contacto está enrolado y en qué paso, sin abrir otra pantalla.
            "secuencia_actual", "secuencia_paso", "secuencia_estado",
        ],
        order_by="modified desc",
        limit=MAX_DEALS,
    )
    leads = frappe.get_all(
        "CRM Lead",
        filters={"contact": contact_name},
        fields=["name", "status", "lead_owner", "organization", "modified"],
        order_by="modified desc",
        limit=MAX_DEALS,
    )

    referencias = [("Contact", contact_name)]
    referencias += [("CRM Deal", d.name) for d in deals]
    referencias += [("CRM Lead", l.name) for l in leads]

    return {
        "found": True,
        "chatwoot": contacto_chatwoot,
        "contact": contacto,
        "deals": deals,
        "leads": leads,
        "notes": _notas_de(referencias),
    }


@frappe.whitelist()
def search_crm_contacts(query: str = "", limit: int = 10) -> list[dict]:
    """Buscador para el diálogo de conversación nueva: por nombre o por teléfono.

    Devuelve solo contactos CON teléfono — sin número no hay a dónde escribirle,
    y ofrecerlos sería un callejón sin salida en la interfaz."""
    validate_role()
    query = (query or "").strip()
    if len(query) < 2:
        return []

    digitos = re.sub(r"\D", "", query)
    if digitos and len(digitos) >= 4:
        filtros = [["mobile_no", "like", f"%{digitos[-10:] if len(digitos) >= 10 else digitos}%"]]
    else:
        filtros = [["name", "like", f"%{query}%"]]

    contactos = frappe.get_all(
        "Contact",
        filters=filtros,
        fields=["name", "first_name", "last_name", "company_name", "mobile_no", "email_id"],
        order_by="modified desc",
        limit=frappe.utils.cint(limit) or 10,
    )
    return [c for c in contactos if c.get("mobile_no")]


# ---------------------------------------------------------------------------
# Fusionar contactos duplicados (T4, 2026-09-23)
#
# Un mismo cliente termina con dos `Contact` y dos conversaciones cuando su
# teléfono entró con y sin el "1" móvil (caso Anabel Osuna: `+524422196109` vs
# `5214422196109`). Chatwoot 4.17.1 ya trae la fusión nativa; antes había que
# hacerlo a mano contacto por contacto.
# ---------------------------------------------------------------------------


def _resolver_contacto_chatwoot(valor) -> tuple[int, str | None]:
    """Normaliza la entrada a `(chatwoot_id, contact_frappe)`.

    Acepta las tres formas con las que el frontend puede mandar un contacto:
      · un **id de Chatwoot** directo (numérico);
      · el **nombre de un `Contact` de Frappe** (se resuelve por su teléfono);
      · un **teléfono** (se busca el homólogo de Chatwoot por últimos 10 dígitos).
    """
    v = str(valor or "").strip()
    if not v:
        frappe.throw("Falta el contacto a fusionar")

    if not v.isdigit() and frappe.db.exists("Contact", v):
        datos_tel = frappe.db.get_value("Contact", v, ["mobile_no", "phone"], as_dict=True) or {}
        cw_contact = _contacto_chatwoot(datos_tel.get("mobile_no") or datos_tel.get("phone"))
        if not cw_contact:
            frappe.throw(f"El contacto {v} no tiene homólogo en Chatwoot (se busca por teléfono).")
        return int(cw_contact["id"]), v

    if v.isdigit():
        return int(v), None

    cw_contact = _contacto_chatwoot(v)
    if not cw_contact:
        frappe.throw(f"No se encontró un contacto de Chatwoot para {v!r}.")
    return int(cw_contact["id"]), _contacto_por_telefono(v)


def _resolver_contacto_chatwoot_suave(valor) -> tuple[int | None, str | None]:
    """Igual que `_resolver_contacto_chatwoot` pero sin lanzar excepción.

    Para la fusión N-a-1 (`fusionar_contactos_multiple`) un contacto sin
    homólogo en Chatwoot no debe abortar la fusión del lado Frappe — el 79%
    de los contactos migrados no tiene match limpio (ver cabecera del módulo),
    y seguiría siendo un duplicado sin cerrar si cualquiera de los dos
    careciera de contacto en Chatwoot."""
    v = str(valor or "").strip()
    if not v:
        return None, None

    if not v.isdigit() and frappe.db.exists("Contact", v):
        datos_tel = frappe.db.get_value("Contact", v, ["mobile_no", "phone"], as_dict=True) or {}
        cw_contact = _contacto_chatwoot(datos_tel.get("mobile_no") or datos_tel.get("phone"))
        return (int(cw_contact["id"]) if cw_contact else None), v

    if v.isdigit():
        return int(v), None

    cw_contact = _contacto_chatwoot(v)
    return (int(cw_contact["id"]) if cw_contact else None), _contacto_por_telefono(v)


def _es_mergee_inexistente(exc: Exception) -> bool:
    """¿El error de Chatwoot es porque el `mergee` ya no existe (una fusión
    anterior sí llegó a ocurrir pero no se alcanzó a registrar)?

    `ChatwootAPIError` no trae el status_code estructurado, solo texto
    (`chatwoot_client._post`) — se detecta por las marcas que deja un 404 en
    el mensaje. Best-effort: si Chatwoot cambia el texto del error, el peor
    caso es que se trate como error real en vez de idempotencia (seguro, solo
    pide reintentar a mano)."""
    msg = str(exc).lower()
    return "404" in msg or "not found" in msg or "couldn't find" in msg or "no encontrado" in msg


def _fusionar_en_chatwoot(base, mergee, lote: str | None = None) -> dict:
    """Fusiona el PAR en Chatwoot y audita como *write-ahead log*: la fila de
    `Chatwoot Fusion` se inserta con `estado="Pendiente"` y se hace commit
    ANTES de llamar a Chatwoot (no después). Así, si `cw.merge_contacts` tiene
    éxito pero el proceso muere antes de marcar `"Chatwoot OK"`, el registro
    ya existe como evidencia y un reintento no vuelve a pegarle a un
    `mergee_id` que Chatwoot ya vació — bug preexistente hasta este cambio,
    donde un fallo del `insert()` posterior a un merge exitoso dejaba el par
    sin registrar (ver `_es_mergee_inexistente`, que detecta justo ese caso en
    el reintento).

    Si alguno de los dos contactos no tiene homólogo en Chatwoot, no aborta:
    devuelve `omitido=True` para que el llamador siga con la parte Frappe."""
    base_id, base_contact = _resolver_contacto_chatwoot_suave(base)
    mergee_id, mergee_contact = _resolver_contacto_chatwoot_suave(mergee)

    if base_id is None or mergee_id is None:
        return {"ok": True, "ya_fusionado": False, "omitido": True, "name": None,
                "base_contact": base_contact, "mergee_contact": mergee_contact,
                "base_chatwoot_id": base_id, "mergee_chatwoot_id": mergee_id}

    if base_id == mergee_id:
        frappe.throw("El contacto base y el duplicado son el mismo.")

    existente = frappe.db.get_value(
        "Chatwoot Fusion",
        {"base_chatwoot_id": base_id, "mergee_chatwoot_id": mergee_id},
        ["name", "estado"], as_dict=True,
    )
    if existente and existente.estado in ("Chatwoot OK", "Completa"):
        return {"ok": True, "ya_fusionado": True, "name": existente.name,
                "base_chatwoot_id": base_id, "mergee_chatwoot_id": mergee_id,
                "base_contact": base_contact, "mergee_contact": mergee_contact}

    if existente:
        nombre_fila = existente.name
    else:
        nombre_fila = frappe.get_doc({
            "doctype": "Chatwoot Fusion",
            "base_chatwoot_id": base_id,
            "mergee_chatwoot_id": mergee_id,
            "base_contact": base_contact,
            "mergee_contact": mergee_contact,
            "mergee_contact_nombre": mergee_contact,
            "fusionado_por": frappe.session.user,
            "fusionado_at": frappe.utils.now(),
            "estado": "Pendiente",
            "lote": lote,
        }).insert(ignore_permissions=True).name
    frappe.db.commit()

    try:
        cw.merge_contacts(base_id, mergee_id)
    except cw.ChatwootAPIError as exc:
        if _es_mergee_inexistente(exc):
            frappe.db.set_value("Chatwoot Fusion", nombre_fila, "estado", "Chatwoot OK")
            frappe.db.commit()
        else:
            frappe.db.set_value("Chatwoot Fusion", nombre_fila,
                                {"estado": "Error", "error": str(exc)[:140]})
            frappe.db.commit()
            raise
    else:
        frappe.db.set_value("Chatwoot Fusion", nombre_fila, "estado", "Chatwoot OK")
        frappe.db.commit()

    return {"ok": True, "ya_fusionado": False, "name": nombre_fila,
            "base_chatwoot_id": base_id, "mergee_chatwoot_id": mergee_id,
            "base_contact": base_contact, "mergee_contact": mergee_contact}


@frappe.whitelist()
def fusionar_contactos(base: str, mergee: str) -> dict:
    """Fusiona dos contactos duplicados en Chatwoot (`base` absorbe a `mergee`).

    Thin wrapper sobre `_fusionar_en_chatwoot`, extraído para reusarse en el
    lote N-a-1 (`fusionar_contactos_multiple`, más abajo). Mismo contrato y
    comportamiento de siempre — `FusionarContactoDialog.vue`/`Contact.vue` no
    se tocaron.
    """
    validate_role()
    base_id, base_contact = _resolver_contacto_chatwoot(base)
    mergee_id, mergee_contact = _resolver_contacto_chatwoot(mergee)
    if base_id == mergee_id:
        frappe.throw("El contacto base y el duplicado son el mismo.")
    return _fusionar_en_chatwoot(base, mergee)


# ---------------------------------------------------------------------------
# Fusionar contactos — selección múltiple (N-a-1, completa: Chatwoot + Frappe)
#
# La fusión de arriba solo absorbe el lado Chatwoot; el `Contact` de Frappe
# duplicado sobrevive con sus Deals/Tasks todavía apuntándole. Esto cierra esa
# parte: fusiona también el `Contact` (vía el merge nativo de Frappe,
# `rename_doc(merge=True)`, ya habilitado — `Contact.allow_rename=1`), con
# selección automática de cuál sobrevive y copia de los datos que se
# perderían al borrar el duplicado.
# ---------------------------------------------------------------------------

MAX_FUSION_LOTE = 10
ROLES_FUSION_EJECUTAR = ("System Manager", "Sales Manager")

PESOS_CAMPO_FUSION = {
    "mobile_no": 3, "email_id": 3, "phone": 1,
    "first_name": 1, "last_name": 1,
    "company_name": 1, "crm_organization": 2,
    "designation": 1, "department": 1, "image": 1,
    "ghl_contact_id": 1, "alias_bancario": 1,
}
PESO_FUSION_DEAL = 10       # CRM Deal.contact
PESO_FUSION_DEAL_SEC = 4    # fila en la child table CRM Contacts
PESO_FUSION_LINK = 2        # fila en Contact.links (Customer, CRM Organization…)
PESO_FUSION_USER = 6        # Contact.user ocupado (portal user colgado del contacto)
PESO_FUSION_CONVERSACION_RECIENTE = 10  # mismo peso que un Deal: quién tiene la
# conversación de Chatwoot más reciente es tan buena señal de "cuál es el
# contacto vivo" como tener una oportunidad abierta — Alejandro, 2026-10-17.

# Escalares que se copian del `mergee` al `base` SOLO si el base los tiene
# vacíos. Deliberadamente NO incluye `email_id`/`phone`/`mobile_no`: son
# derivados (`Contact.validate()` los recalcula desde `email_ids`/`phone_nos`
# y los vacía si esas child tables están vacías — escribirlos es un no-op
# que `Contact.validate()` revierte solo). Tampoco incluye `google_contacts_id`
# (es el id del MERGEE en Google People; copiarlo pisaría el contacto
# equivocado en el próximo sync) ni `is_primary_contact` (puede disparar
# `validate_primary_contact` sobre otros contactos, efecto fuera de alcance).
_ESCALARES_PRECOPIABLES_CONTACT = (
    "first_name", "last_name", "middle_name", "salutation", "gender",
    "designation", "department", "company_name", "crm_organization",
    "address", "image", "user", "ghl_contact_id", "alias_bancario",
    "tipo_relacion", "cliente_slug",
)
_RANGO_STATUS_CONTACT = {"Replied": 2, "Open": 1, "Passive": 0}

# Doctypes propios de este proyecto (frappe_chatwoot/crm) que vinculan a
# Contact por Dynamic Link. El merge nativo de Frappe ya reasigna estos campos
# vía `rename_dynamic_links`, pero ese mapa (`get_dynamic_link_map`) se cachea
# 12h y el propio core lo documenta como "posiblemente desactualizado" — una
# entrada vieja (plausible en un doctype agregado después del último rebuild
# del caché, como `CRM Notification`) deja un huérfano silencioso. Por eso se
# reasignan explícito, ANTES del `rename_doc`, para que además el chequeo de
# links de `delete_doc` no tropiece con uno que el caché no conocía.
#
# Deliberadamente NO incluye `ToDo`: el merge nativo ya tiene manejo propio
# para esa tabla (`update_assignments`, en `rename_doc.py`) que deduplica
# recordatorios cuando el mismo usuario tenía asignación tanto en el base como
# en el mergee — reasignarla a mano aquí adelantaría el `reference_name` y
# dejaría a ese dedup sin nada que encontrar, resultando en ToDos duplicados.
# Tampoco incluye `Communication`/`Comment`/`Communication Link`: son
# doctypes de Frappe core, con años de uso — el caso real de caché
# desactualizada aplica a los doctypes propios de este proyecto, no a esos.
_REFERENCIAS_DINAMICAS_FUSION = {
    "FCRM Note": ("reference_doctype", "reference_docname"),
    "CRM Task": ("reference_doctype", "reference_docname"),
    "CRM Call Log": ("reference_doctype", "reference_docname"),
    "CRM Notification": ("reference_doctype", "reference_name"),
}


def _validar_rol_fusion():
    """La fusión N-a-1 reasigna Deals/Tasks de varios contactos a la vez y es
    irreversible del lado Chatwoot — el gate de `validate_role()` (hasta
    `Sales User`) alcanza para la vista previa, pero no para ejecutarla."""
    validate_role()
    user_roles = frappe.get_roles(frappe.session.user)
    if not any(r in user_roles for r in ROLES_FUSION_EJECUTAR) and frappe.session.user != "Administrator":
        frappe.throw(
            "Fusionar varios contactos requiere rol Sales Manager o System Manager.",
            frappe.PermissionError,
        )


def _parse_contactos(contactos, *, exigir_existencia: bool = True) -> list[str]:
    """El frontend manda la selección como JSON (mismo patrón que `_parse_adjuntos`,
    arriba, para listas que vienen de la UI). Deduplica conservando el orden
    y exige que el tamaño del lote sea sano.

    `exigir_existencia=False` (usado por `fusionar_contactos_multiple`, para
    que el reintento de un lote parcialmente fusionado sea idempotente): un
    `mergee` de un intento anterior ya no existe como `Contact` — eso es
    justo la señal de "ya_fusionado", no un error. La vista previa sí exige
    que todos existan: ahí no hay reintento, solo una selección fresca de la
    lista."""
    if isinstance(contactos, str):
        try:
            contactos = json.loads(contactos or "[]")
        except ValueError:
            frappe.throw("Lista de contactos inválida")
    nombres = list(dict.fromkeys(str(c).strip() for c in (contactos or []) if str(c).strip()))
    if len(nombres) < 2:
        frappe.throw("Selecciona al menos 2 contactos para fusionar")
    if len(nombres) > MAX_FUSION_LOTE:
        frappe.throw(f"Máximo {MAX_FUSION_LOTE} contactos por fusión")
    if exigir_existencia:
        faltantes = [n for n in nombres if not frappe.db.exists("Contact", n)]
        if faltantes:
            frappe.throw(f"No existen estos contactos: {', '.join(faltantes)}")
    return nombres


def _ultima_actividad_chatwoot(name: str) -> int:
    """Epoch de la conversación de Chatwoot más reciente del contacto, o 0 si
    no tiene homólogo, no tiene conversaciones, o la API falla — best-effort,
    nunca bloquea la elección de base por una caída de Chatwoot (mismo
    criterio de degradar suave que `_fusionar_en_chatwoot`/`_resolver_contacto_
    chatwoot_suave`, dado que el 79% de los contactos migrados no tiene match
    limpio)."""
    try:
        chatwoot_id, _ = _resolver_contacto_chatwoot_suave(name)
        if chatwoot_id is None:
            return 0
        conversaciones = cw.get_conversations_for_contact(chatwoot_id)
    except cw.ChatwootAPIError:
        return 0
    if not conversaciones:
        return 0
    return max((c.get("last_activity_at") or 0) for c in conversaciones)


def _puntuar_contacto(name: str) -> dict:
    """Score de completitud/relevancia para decidir cuál de varios duplicados
    sobrevive. Pondera mucho más fuerte lo que SÍ se perdería si el contacto
    se borra sin pre-copiarlo primero (oportunidades vinculadas, vínculos
    dinámicos, usuario de portal) que la mera cantidad de campos llenos."""
    campos = list(PESOS_CAMPO_FUSION) + ["full_name", "creation"]
    c = frappe.db.get_value("Contact", name, campos, as_dict=True) or {}

    deals = frappe.db.count("CRM Deal", {"contact": name})
    deals_sec = frappe.db.count("CRM Contacts", {"contact": name})
    links = frappe.db.count("Dynamic Link", {"parenttype": "Contact", "parent": name})

    score = sum(peso for campo, peso in PESOS_CAMPO_FUSION.items() if c.get(campo))
    score += deals * PESO_FUSION_DEAL + deals_sec * PESO_FUSION_DEAL_SEC + links * PESO_FUSION_LINK
    score += PESO_FUSION_USER if c.get("user") else 0

    return {
        "name": name,
        "full_name": c.get("full_name"),
        "mobile_no": c.get("mobile_no"),
        "email_id": c.get("email_id"),
        "company_name": c.get("company_name"),
        "crm_organization": c.get("crm_organization"),
        "creation": c.get("creation"),
        "deals": deals,
        "deals_sec": deals_sec,
        "links": links,
        "ultima_conversacion": _ultima_actividad_chatwoot(name),
        "score": score,
    }


def _razon_de(c: dict) -> str:
    """Texto humano para el diálogo ("3 oportunidades, correo y organización,
    conversación más reciente, creado 2026-03-11") — explica POR QUÉ se
    recomendó ese contacto como base, en vez de mostrar un número de score sin
    contexto."""
    total_deals = c["deals"] + c["deals_sec"]
    partes = []
    if total_deals:
        partes.append(f"{total_deals} oportunidad{'es' if total_deals != 1 else ''}")
    if c.get("email_id"):
        partes.append("correo")
    if c.get("crm_organization") or c.get("company_name"):
        partes.append("organización")
    if c.get("_conversacion_mas_reciente"):
        partes.append("conversación más reciente")
    detalle = ", ".join(partes) if partes else "sin datos adicionales"
    fecha = frappe.utils.formatdate(c.get("creation")) if c.get("creation") else "—"
    return f"{detalle}, creado {fecha}"


def _elegir_contacto_base(contactos: list[str]) -> dict:
    """Heurística determinista: a igual score gana el más antiguo
    (`creation` asc); a igual antigüedad gana el `name` menor. Así la vista
    previa y la ejecución nunca pueden elegir distinto para el mismo
    conjunto, sin importar el orden en que llegó la selección.

    Quién tiene la conversación de Chatwoot más reciente (`ultima_conversacion`,
    resuelta en `_puntuar_contacto`) suma el mismo peso que una oportunidad
    abierta (`PESO_FUSION_CONVERSACION_RECIENTE`) — es una señal adicional de
    "cuál es el contacto vivo", no un criterio que por sí solo decida; se
    combina con el resto del score en vez de reemplazarlo. Esto SÍ dispara
    hasta 2 llamadas HTTP a Chatwoot por candidato (resolver id + listar
    conversaciones) — hasta `MAX_FUSION_LOTE` candidatos en la vista previa.
    Antes de este cambio la vista previa no tocaba red más que para el
    homólogo del `base`; se acepta la latencia porque es una acción manual y
    poco frecuente, y porque sin ella no hay forma de saber quién tiene la
    conversación más reciente."""
    candidatos = [_puntuar_contacto(n) for n in contactos]
    max_actividad = max((c["ultima_conversacion"] for c in candidatos), default=0)
    if max_actividad:
        for c in candidatos:
            if c["ultima_conversacion"] == max_actividad:
                c["score"] += PESO_FUSION_CONVERSACION_RECIENTE
                c["_conversacion_mas_reciente"] = True
    candidatos.sort(key=lambda c: (-c["score"], c["creation"] or "", c["name"]))
    for c in candidatos:
        c["razon"] = _razon_de(c)
    base = candidatos[0]
    return {"base": base["name"], "candidatos": candidatos, "razon": base["razon"]}


def _precopiar_datos(base_doc, mergee_name: str) -> list[str]:
    """Antes de que `rename_doc(merge=True)` borre el `mergee`, copia al
    `base` lo que se perdería con él. Child tables por valor normalizado
    (teléfono por últimos 10 dígitos — mismo criterio que `tel.corto()` ya
    usa el proyecto para no duplicar a Anabel Osuna otra vez; email en
    minúsculas), nunca duplicando lo que el base ya tiene. `unsubscribed` se
    combina con OR: si cualquiera de los dos estaba dado de baja, el base
    queda dado de baja — nunca resuscribir a alguien sin querer. Un solo
    `base_doc.save()` lo deja todo persistido antes del merge.
    """
    mergee = frappe.get_doc("Contact", mergee_name)
    copiado = []

    for campo in _ESCALARES_PRECOPIABLES_CONTACT:
        if not base_doc.get(campo) and mergee.get(campo):
            base_doc.set(campo, mergee.get(campo))
            copiado.append(campo)

    cortos_existentes = {tel.corto(p.phone) for p in base_doc.phone_nos if tel.corto(p.phone)}
    for fila in mergee.phone_nos:
        corto = tel.corto(fila.phone)
        if corto and corto not in cortos_existentes:
            base_doc.append("phone_nos", {
                "phone": fila.phone, "is_primary_phone": 0, "is_primary_mobile_no": 0,
            })
            cortos_existentes.add(corto)
            copiado.append(f"teléfono {fila.phone}")

    correos_existentes = {e.email_id.strip().lower() for e in base_doc.email_ids if e.email_id}
    for fila in mergee.email_ids:
        correo = (fila.email_id or "").strip().lower()
        if correo and correo not in correos_existentes:
            base_doc.append("email_ids", {"email_id": fila.email_id, "is_primary": 0})
            correos_existentes.add(correo)
            copiado.append(f"correo {fila.email_id}")

    links_existentes = {(l.link_doctype, l.link_name) for l in base_doc.links}
    for fila in mergee.links:
        clave = (fila.link_doctype, fila.link_name)
        if clave not in links_existentes:
            base_doc.append("links", {"link_doctype": fila.link_doctype, "link_name": fila.link_name})
            links_existentes.add(clave)
            copiado.append(f"vínculo {fila.link_doctype} {fila.link_name}")

    if mergee.get("unsubscribed") and not base_doc.get("unsubscribed"):
        base_doc.unsubscribed = 1
        copiado.append("unsubscribed")

    if _RANGO_STATUS_CONTACT.get(mergee.get("status"), -1) > _RANGO_STATUS_CONTACT.get(base_doc.get("status"), -1):
        base_doc.status = mergee.get("status")
        copiado.append("status")

    return copiado


def _reasignar_referencias_dinamicas(mergee_name: str, base_name: str) -> dict:
    """Barrido explícito de Dynamic Link antes del `rename_doc` — ver el
    comentario de `_REFERENCIAS_DINAMICAS_FUSION` arriba sobre por qué estos
    doctypes y no otros. Update directo por SQL (sin disparar hooks propios de
    esos doctypes, igual que hace el core)."""
    conteos = {}
    for doctype, (campo_tipo, campo_nombre) in _REFERENCIAS_DINAMICAS_FUSION.items():
        n = frappe.db.count(doctype, {campo_tipo: "Contact", campo_nombre: mergee_name})
        if n:
            frappe.db.set_value(
                doctype, {campo_tipo: "Contact", campo_nombre: mergee_name},
                campo_nombre, base_name, update_modified=False,
            )
            conteos[doctype] = n
    return conteos


def _reasignar_secuencia_inscripcion(mergee_name: str, base_name: str) -> list[str]:
    """`Secuencia Inscripcion.contacto` es un Link normal (no Dynamic Link) —
    si no se reasigna queda huérfano igual que cualquier otro, así que SIEMPRE
    se reasigna. Pero si el `base` YA estaba inscrito en la misma secuencia,
    queda una inscripción duplicada que NO se borra sola aquí: cada fila tiene
    estado propio (`paso_actual`, `proximo_en`) y adivinar cuál conservar
    podría saltarse o repetir un envío — se reporta para revisión manual en
    vez de autocorregirse."""
    filas_mergee = frappe.get_all(
        "Secuencia Inscripcion", filters={"contacto": mergee_name},
        fields=["name", "secuencia"],
    )
    if not filas_mergee:
        return []
    secuencias_base = set(frappe.get_all(
        "Secuencia Inscripcion", filters={"contacto": base_name},
        pluck="secuencia",
    ))
    duplicadas = []
    for fila in filas_mergee:
        frappe.db.set_value("Secuencia Inscripcion", fila.name, "contacto", base_name,
                            update_modified=False)
        if fila.secuencia in secuencias_base:
            duplicadas.append(f"{fila.name} (secuencia {fila.secuencia})")
    return duplicadas


def _dedupe_crm_contacts(base_name: str) -> int:
    """Tras el merge, un `CRM Deal` que ya tenía tanto al base como al mergee
    en su child table `CRM Contacts` queda con dos filas iguales (el
    `update_link_field_values` del core reasigna el Link de las dos a la
    vez). Sin estado propio — son solo un espejo de datos del Contact
    (`fetch_from`) —, así que borrar el sobrante es seguro, a diferencia de
    `Secuencia Inscripcion`."""
    filas = frappe.get_all(
        "CRM Contacts", filters={"contact": base_name},
        fields=["name", "parent", "idx", "is_primary"],
    )
    por_deal: dict[str, list] = {}
    for f in filas:
        por_deal.setdefault(f.parent, []).append(f)
    borradas = 0
    for grupo in por_deal.values():
        if len(grupo) < 2:
            continue
        grupo.sort(key=lambda f: (0 if f.is_primary else 1, f.idx))
        for sobrante in grupo[1:]:
            frappe.db.delete("CRM Contacts", {"name": sobrante.name})
            borradas += 1
    return borradas


@frappe.whitelist()
def previsualizar_fusion_contactos(contactos) -> dict:
    """Vista previa, sin efectos secundarios: ninguna escritura, pero SÍ hace
    red — `_elegir_contacto_base` (vía `_puntuar_contacto`) resuelve la
    conversación de Chatwoot más reciente de cada candidato, hasta 2 llamadas
    HTTP por candidato y hasta `MAX_FUSION_LOTE` candidatos (ver su docstring).
    Antes de agregar ese criterio esto era 1 sola llamada (el homólogo del
    `base`); se acepta la latencia adicional porque es una acción manual y
    poco frecuente. El frontend manda este mismo `base` de vuelta al ejecutar
    (`fusionar_contactos_multiple`), así que preview y ejecución nunca pueden
    discrepar por una carrera de datos entre los dos clics."""
    validate_role()
    nombres = _parse_contactos(contactos)
    eleccion = _elegir_contacto_base(nombres)

    base_chatwoot_id, _ = _resolver_contacto_chatwoot_suave(eleccion["base"])
    advertencias = []
    if base_chatwoot_id is None:
        advertencias.append(
            "El contacto base no tiene homólogo en Chatwoot — la fusión solo afectará el CRM."
        )
    sin_chatwoot = [
        c["name"] for c in eleccion["candidatos"]
        if c["name"] != eleccion["base"] and not c.get("mobile_no") and not c.get("email_id")
    ]
    if sin_chatwoot:
        advertencias.append(
            f"{len(sin_chatwoot)} contacto(s) sin teléfono/correo — probablemente sin "
            "homólogo en Chatwoot; de todos modos se fusionarán en el CRM."
        )

    return {
        "base": eleccion["base"],
        "razon": eleccion["razon"],
        "candidatos": eleccion["candidatos"],
        "base_chatwoot_id": base_chatwoot_id,
        "advertencias": advertencias,
    }


@frappe.whitelist()
def fusionar_contactos_multiple(contactos, base: str) -> dict:
    """Fusiona N contactos duplicados en uno (`base`, Chatwoot + Frappe).

    `base` debe ser exactamente el que devolvió `previsualizar_fusion_contactos`
    (o el que el usuario eligió a mano en el diálogo, anulando la
    recomendación) — nunca se recalcula a ciegas aquí, para que preview y
    ejecución no puedan discrepar. Cada `mergee` es su propia unidad
    transaccional: un fallo no aborta el resto del lote, y un `mergee` que ya
    no existe como `Contact` (fusionado en un intento anterior) se reporta
    como `ya_fusionado` sin volver a tocar Chatwoot."""
    _validar_rol_fusion()
    nombres = _parse_contactos(contactos, exigir_existencia=False)
    base = (base or "").strip()
    if base not in nombres:
        frappe.throw("El contacto base debe estar dentro de los seleccionados")
    if not frappe.db.exists("Contact", base):
        frappe.throw(f"El contacto base {base} no existe")

    # Recalculado solo sobre los que SIGUEN existiendo: un mergee ya fusionado
    # en un intento anterior no debe competir por la recomendación.
    existentes = [n for n in nombres if n == base or frappe.db.exists("Contact", n)]
    recalculo = _elegir_contacto_base(existentes)
    base_recomendado_cambio = recalculo["base"] != base

    lote = frappe.generate_hash(length=10)
    resultados = []
    for mergee in [n for n in nombres if n != base]:
        resultado = {"mergee": mergee}
        if not frappe.db.exists("Contact", mergee):
            resultado.update(ok=True, ya_fusionado=True)
            resultados.append(resultado)
            continue

        fusion_cw = None
        try:
            fusion_cw = _fusionar_en_chatwoot(base, mergee, lote=lote)

            base_doc = frappe.get_doc("Contact", base)
            datos_copiados = _precopiar_datos(base_doc, mergee)
            base_doc.flags.fusion_en_curso = True
            base_doc.flags.ignore_mandatory = True
            base_doc.save(ignore_permissions=True)

            _reasignar_referencias_dinamicas(mergee, base)
            duplicadas_secuencia = _reasignar_secuencia_inscripcion(mergee, base)

            from frappe.model.rename_doc import rename_doc as _rename_doc

            _rename_doc("Contact", mergee, base, merge=True, ignore_permissions=True,
                        show_alert=False, rebuild_search=False)

            borradas = _dedupe_crm_contacts(base)

            if fusion_cw.get("name"):
                frappe.db.set_value("Chatwoot Fusion", fusion_cw["name"], {
                    "estado": "Completa",
                    "contact_frappe_merged": 1,
                    "lote": lote,
                    "datos_copiados": "; ".join(datos_copiados) or None,
                })
            frappe.db.commit()

            resultado.update(
                ok=True, ya_fusionado=False,
                chatwoot_omitido=bool(fusion_cw.get("omitido")),
                datos_copiados=datos_copiados,
                crm_contacts_deduplicadas=borradas,
                secuencias_duplicadas=duplicadas_secuencia,
            )
        except Exception as exc:
            frappe.db.rollback()
            frappe.log_error(title="fusionar_contactos_multiple", message=frappe.get_traceback())
            if fusion_cw and fusion_cw.get("name"):
                frappe.db.set_value("Chatwoot Fusion", fusion_cw["name"],
                                    {"estado": "Error", "error": str(exc)[:140]})
                frappe.db.commit()
            resultado.update(ok=False, ya_fusionado=False, error=str(exc))
        resultados.append(resultado)

    ok = sum(1 for r in resultados if r.get("ok"))
    fallidos = sum(1 for r in resultados if not r.get("ok"))
    return {
        "base": base, "lote": lote, "resultados": resultados,
        "ok": ok, "fallidos": fallidos,
        "base_recomendado_cambio": base_recomendado_cambio,
        "base_recomendado": recalculo["base"],
    }


# ---------------------------------------------------------------------------
# Abrir conversación nueva
# ---------------------------------------------------------------------------


def _config_evolution() -> dict:
    """URL, instancia y api key de Evolution, guardadas como custom fields de
    Chatwoot Settings (no en el .env: el contenedor de Frappe no lo comparte)."""
    settings = frappe.get_single("Chatwoot Settings")
    return {
        "url": (getattr(settings, "evolution_url", None) or "").rstrip("/"),
        "instancia": getattr(settings, "evolution_instance", None) or "",
        "api_key": settings.get_password("evolution_api_key", raise_exception=False)
        if getattr(settings, "evolution_api_key", None)
        else "",
    }


def _e164_mx(telefono: str) -> tuple[str, str]:
    """(dígitos, E.164). 10 dígitos se asumen mexicanos: es lo que el equipo
    teclea. Con lada de país ya incluida se respeta tal cual."""
    digitos = re.sub(r"\D", "", telefono or "")
    if len(digitos) == 10:
        digitos = "52" + digitos
    if not (10 <= len(digitos) <= 15):
        frappe.throw("Número inválido: escribe 10 dígitos (México) o el número con lada de país.")
    return digitos, "+" + digitos


@frappe.whitelist()
def resolver_whatsapp(phone: str) -> dict:
    """¿Este número existe en WhatsApp, y con qué JID?

    Se le pregunta a WhatsApp a través de Evolution en vez de construir el JID
    a mano: los números mexicanos llevan un "1" después del 52 que no todos
    tienen, y adivinarlo abre una conversación con un destinatario inexistente
    — el mensaje se queda en el limbo sin error visible.

    Si Evolution no responde, devuelve `verificado: False` en vez de fallar: se
    puede seguir adelante sin JID (Chatwoot resuelve por número), solo sin la
    garantía previa. Nunca bloquea al usuario por una dependencia caída.
    """
    validate_role()
    digitos, e164 = _e164_mx(phone)
    cfg = _config_evolution()
    if not (cfg["url"] and cfg["instancia"] and cfg["api_key"]):
        return {"verificado": False, "existe": None, "jid": None, "e164": e164,
                "motivo": "Evolution no configurado en Chatwoot Settings"}
    try:
        req = urllib.request.Request(
            f"{cfg['url']}/chat/whatsappNumbers/{cfg['instancia']}",
            data=json.dumps({"numbers": [digitos]}).encode(),
            headers={"Content-Type": "application/json", "apikey": cfg["api_key"]},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            datos = json.loads(resp.read().decode())
        fila = (datos or [{}])[0]
        return {
            "verificado": True,
            "existe": bool(fila.get("exists")),
            "jid": fila.get("jid"),
            "e164": e164,
        }
    except (urllib.error.URLError, TimeoutError, ValueError, IndexError) as exc:
        frappe.log_error(title="panel: Evolution no responde", message=str(exc))
        return {"verificado": False, "existe": None, "jid": None, "e164": e164,
                "motivo": f"Evolution no responde: {exc}"}


@frappe.whitelist()
def start_conversation(inbox_id: int, phone: str, message: str = None, name: str = None,
                       force: int = 0, adjuntos=None,
                       deal: str = None, lead: str = None) -> dict:
    """Abre (o reutiliza) la conversación con ese número y manda el primer mensaje.

    Deja el agente IA PAUSADO: quien abre la conversación desde el CRM es una
    persona que se está haciendo cargo del hilo.

    `adjuntos` (opcional): lista JSON de `{url, nombre, mime}` — archivos ya
    subidos al sitio por el composer de la bandeja. Se mandan como media nativa;
    sin esto la imagen llega como enlace pegado al texto.
    """
    validate_role()
    adjuntos = _parse_adjuntos(adjuntos)
    message = (message or "").strip()
    if not message and not adjuntos:
        frappe.throw("Escribe el mensaje con el que quieres abrir la conversación.")
    return abrir_conversacion(inbox_id=inbox_id, phone=phone, message=message,
                              name=name, force=force, pausar=True, adjuntos=adjuntos,
                              deal=deal, lead=lead)


@frappe.whitelist()
def asegurar_conversacion(inbox_id: int, phone: str, name: str = None,
                          force: int = 0, deal: str = None, lead: str = None) -> dict:
    """Crea (o reutiliza) la conversación SIN mandar mensaje.

    Existe por el "programar" del primer mensaje: `programar_mensaje` exige un
    `conversation_id` y en modo "nueva conversación" todavía no hay ninguno. Se
    crea el hilo y ahí se agenda. Es el único caso que deja un hilo sin mensajes
    — y es una acción explícita del usuario, no un efecto colateral.
    """
    validate_role()
    return abrir_conversacion(inbox_id=inbox_id, phone=phone, message=None,
                              name=name, force=force, pausar=True,
                              deal=deal, lead=lead)


def _parse_adjuntos(adjuntos) -> list:
    if isinstance(adjuntos, str):
        try:
            adjuntos = json.loads(adjuntos or "[]")
        except ValueError:
            return []
    adjuntos = adjuntos or []
    if len(adjuntos) > MAX_ADJUNTOS:
        frappe.throw(f"Máximo {MAX_ADJUNTOS} adjuntos por mensaje")
    return adjuntos


def _enviar(conversation_id: int, message: str | None, adjuntos: list | None) -> None:
    """Manda un mensaje al hilo, como media nativa si trae adjuntos.

    Mismo camino que `api/chatwoot.send_message`: con la URL pegada al `content`
    el cliente ve un enlace, no la imagen (ver `chatwoot_client.leer_adjunto`).
    Se marca `humano` (con el usuario de la sesión) para que el agente lea el
    mensaje como palabra del equipo y la burbuja muestre quién lo escribió.
    Sin texto ni adjuntos no hace nada: es el caso de `asegurar_conversacion`.
    """
    marca = autoria.marca_humana()
    adjuntos = adjuntos or []
    if adjuntos:
        archivos = []
        for a in adjuntos:
            leido = cw.leer_adjunto(a.get("url") or a.get("archivo"))
            if leido:
                archivos.append(leido)
        if archivos:
            cw.create_message_with_attachments(
                conversation_id, message or "", archivos=archivos,
                content_attributes=marca)
            return
        if not message:
            frappe.throw("No se pudieron leer los adjuntos")
    if message:
        cw.create_message(conversation_id, message, content_attributes=marca)


def _buscar_hilo_del_contacto(contacto_id: int, inbox_id: int) -> dict | None:
    """Hilo del contacto en CUALQUIER inbox del sitio (no solo el elegido).

    Un mismo contacto puede haber escrito por más de un canal del mismo sitio
    (WhatsApp + Messenger). Reusar solo el hilo del inbox elegido abría un
    segundo hilo con la misma persona — justo lo que `abrir_conversacion` viene
    a evitar. Se restringe a los inboxes del sitio (`_inboxes_del_sitio`), nunca
    a la cuenta completa de Chatwoot: la misma cuenta sirve a varios clientes.

    Preferencia: el mismo inbox que el pedido → un hilo activo (`open`/`pending`)
    → el de actividad más reciente.
    """
    sitio = set(_inboxes_del_sitio())
    sitio.add(frappe.utils.cint(inbox_id))
    candidatos = [
        c for c in cw.get_conversations_for_contact(contacto_id)
        if frappe.utils.cint(c.get("inbox_id")) in sitio
    ]
    if not candidatos:
        return None
    return max(
        candidatos,
        key=lambda c: (
            1 if frappe.utils.cint(c.get("inbox_id")) == frappe.utils.cint(inbox_id) else 0,
            1 if c.get("status") in ("open", "pending") else 0,
            c.get("last_activity_at") or c.get("created_at") or 0,
        ),
    )


def _escribir_conversation_id(conversation_id: int, deal=None, lead=None,
                              phone: str = None) -> None:
    """Escribe el id del hilo en el `CRM Deal`/`CRM Lead` de origen.

    El campo `chatwoot_conversation_id` es lo que le permite al CRM volver a
    abrir el MISMO hilo en vez de crear otro (contrato con el SPA, 2026-09-28);
    antes de esto nadie lo escribía en el flujo de la bandeja. Verificado en
    `tabCustom Field`: existe en ambos doctypes, tipo Data.

    Si el llamador dice cuál es el registro (`deal`/`lead`) se escribe ese; si
    no, se resuelve por teléfono y se completan SOLO los registros que no tengan
    ya un hilo anotado — así no se pisa una conversación distinta ya escrita.
    """
    objetivos: list[tuple[str, str]] = []
    if deal:
        objetivos.append(("CRM Deal", deal))
    if lead:
        objetivos.append(("CRM Lead", lead))
    if not objetivos:
        contacto = _contacto_por_telefono(phone)
        if not contacto:
            return
        for doctype in ("CRM Deal", "CRM Lead"):
            filas = frappe.get_all(
                doctype,
                filters={"contact": contacto},
                fields=["name", "chatwoot_conversation_id"],
            )
            objetivos.extend(
                (doctype, f.name) for f in filas
                if not (f.chatwoot_conversation_id or "").strip()
            )
    for doctype, name in objetivos:
        if name and frappe.db.exists(doctype, name):
            frappe.db.set_value(doctype, name, "chatwoot_conversation_id",
                                str(conversation_id), update_modified=False)


def abrir_conversacion(inbox_id: int, phone: str, message: str = None,
                       name: str = None, force: int = 0,
                       pausar: bool = True, adjuntos: list | None = None,
                       deal: str = None, lead: str = None) -> dict:
    """Abre (o reutiliza) la conversación con ese número.

    Reutiliza a propósito: si el contacto ya tiene un hilo abierto en ese inbox,
    escribe ahí en vez de abrir uno nuevo. Dos hilos con la misma persona es el
    escenario que hace que un cliente reciba dos respuestas distintas del mismo
    equipo — el problema que ya conocemos por GHL.

    `pausar=False` es para los avisos automáticos (bienvenida del formulario):
    ahí el agente IA debe poder seguir atendiendo, porque nadie del equipo se
    hizo cargo todavía. Sin verificación de rol: la llaman jobs del sistema,
    no la UI.

    `message` y `adjuntos` son opcionales: sin ninguno de los dos solo se
    asegura que el hilo exista (ver `asegurar_conversacion`).

    Reutiliza el hilo del contacto en CUALQUIER inbox del sitio (no solo el
    elegido) y reabre uno `resolved`/`snoozed` en vez de crear un segundo. El
    id resultante se escribe de vuelta en el `CRM Deal`/`CRM Lead` de origen
    (`chatwoot_conversation_id`), para que el CRM abra el mismo hilo.
    """
    inbox_id = frappe.utils.cint(inbox_id)
    message = (message or "").strip()
    if not inbox_id:
        frappe.throw("Falta el inbox.")

    digitos, e164 = _e164_mx(phone)
    verificacion = resolver_whatsapp(phone)
    if verificacion.get("verificado") and not verificacion.get("existe") and not frappe.utils.cint(force):
        frappe.throw(f"El número {e164} no tiene WhatsApp. Verifícalo antes de escribir.")
    jid = verificacion.get("jid")

    # WhatsApp México usa `+521` + 10 dígitos, pero `_e164_mx` arma `+52` + 10
    # (no adivina el "1" a propósito). Evolution sí resuelve el número real y
    # devuelve el JID; de ahí se saca la forma correcta. Si el contacto queda
    # guardado como `+52` sin el "1", Evolution no lo encuentra y los mensajes
    # ENTRANTES se pierden (bug del 2026-09-10, que el vigilante reparaba hasta
    # 10 min después). Por eso el alta usa el número del JID cuando lo hay.
    e164_whatsapp = e164
    if jid and jid.endswith("@s.whatsapp.net"):
        e164_whatsapp = "+" + jid.split("@")[0]

    # Chatwoot busca por el número tal cual esté guardado; se prueban las formas
    # en que puede haber quedado (con y sin +, con y sin el 1 de México).
    contacto = None
    for consulta in dict.fromkeys((e164_whatsapp, e164, digitos, digitos[-10:])):
        encontrados = cw.search_contacts(consulta)
        if encontrados:
            contacto = encontrados[0]
            break

    if contacto:
        contacto_id = contacto.get("id")
        # ¿Ya hay hilo con este contacto en algún canal del sitio? Se escribe ahí.
        hilo = _buscar_hilo_del_contacto(contacto_id, inbox_id)
        if hilo:
            conversation_id = frappe.utils.cint(hilo.get("id"))
            reabierta = hilo.get("status") not in ("open", "pending")
            if reabierta:
                cw.toggle_conversation_status(conversation_id, "open")
            _enviar(conversation_id, message, adjuntos)
            if pausar:
                _pause_conversation(conversation_id, hilo.get("inbox_id"))
            _escribir_conversation_id(conversation_id, deal=deal, lead=lead, phone=e164)
            return {"conversation_id": conversation_id, "reutilizada": True,
                    "contact_id": contacto_id, "reabierta": reabierta}

        # Sin hilo reutilizable: se abre uno nuevo en el inbox elegido.
        detalle = cw.get_contact(contacto_id)
        buzones = detalle.get("contact_inboxes") or []
        source_id = next(
            (b.get("source_id") for b in buzones if ((b.get("inbox") or {}).get("id")) == inbox_id),
            None,
        )
        if not source_id:
            creado = cw.create_contact_inbox(contact_id=contacto_id, inbox_id=inbox_id, source_id=jid)
            source_id = (creado.get("payload") or creado).get("source_id") or jid
    else:
        nuevo = cw.create_contact(
            inbox_id=inbox_id,
            name=(name or "").strip() or e164,
            phone_number=e164_whatsapp,
            identifier=jid,
        )
        contacto_id = nuevo.get("id")
        buzones = nuevo.get("contact_inboxes") or []
        source_id = (buzones[0].get("source_id") if buzones else None) or jid

    if not source_id:
        frappe.throw("Chatwoot no devolvió el canal del contacto; no se puede abrir la conversación.")

    conv = cw.create_conversation(source_id=source_id, inbox_id=inbox_id, contact_id=contacto_id)
    conversation_id = conv.get("id") or (conv.get("payload") or {}).get("id")
    if not conversation_id:
        frappe.throw("Chatwoot no devolvió el id de la conversación creada.")

    _enviar(conversation_id, message, adjuntos)
    if pausar:
        _pause_conversation(conversation_id, inbox_id)
    _escribir_conversation_id(conversation_id, deal=deal, lead=lead, phone=e164)
    return {"conversation_id": conversation_id, "reutilizada": False, "contact_id": contacto_id}
