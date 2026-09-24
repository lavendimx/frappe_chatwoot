"""Autofollowup: reactivación de conversaciones dejadas en visto (2026-09-24).

Réplica del bot action `advancedFollowup` ("Auto Follow-Up") de GHL Conversation
AI. Plan: `nuevosofia/planes/autofollowup-reactivacion-conversaciones.md`.

Este módulo empieza SOLO con el clasificador (paso 3 del plan) — de solo
lectura, no escribe nada. Es el gate: antes de construir el motor de envío se
mide cuántos hilos mudos son de verdad elegibles (prospecto, sin secuencia, sin
humano reciente). Si el número es bajo, el módulo no compensa el riesgo de la
ráfaga sobre el canal Baileys y el plan se replantea.

CRITERIOS DE EXCLUSIÓN (en este orden, el primero que aplica manda):
  1. El último mensaje real del hilo es de un humano del equipo (marca
     `content_attributes` de `utils/autoria.py`, o heredado sin marca con
     `message_type=outgoing` que no sea del bot ni de un automatismo) — alguien
     ya está atendiendo esa conversación a mano.
  2. El contacto es CLIENTE ACTIVO: su `Contact.ghl_contact_id` resuelve a un
     `Customer` de ERPNext (mismo vínculo que `facturacion.cartera_de_contacto`
     — ver `vincular_customer_contacto.py`). Un cliente hablando de facturación
     o soporte no es un prospecto que reactivar.
  3. Ya tiene una `Secuencia Inscripcion` en estado Activa — evita el doble
     mensaje con el motor de secuencias existente (p. ej. "4. Seguimientos —
     PVP").
  4. El deal ya está cerrado (`Won`/`Lost`).

Sin deal ligado a la conversación (`chatwoot_conversation_id`), NO se excluye
por defecto — son leads nuevos aún sin oportunidad, que sí son candidatos.
"""

import json
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import frappe

from . import autoria, chatwoot_client


def _es_cliente(contact_name: str | None) -> bool:
    """Mismo vínculo que `facturacion.cartera_de_contacto`: Contact.ghl_contact_id
    -> Customer.ghl_contact_id. Sin vínculo, NO se asume cliente (falso negativo
    es más seguro aquí: perdemos un follow-up, no arriesgamos molestar a un
    cliente que paga)."""
    if not contact_name:
        return False
    ghl_id = frappe.db.get_value("Contact", contact_name, "ghl_contact_id")
    if not ghl_id:
        return False
    return bool(frappe.db.get_value("Customer", {"ghl_contact_id": ghl_id}, "name"))


def _deal_de(conversation_id) -> dict | None:
    return frappe.db.get_value(
        "CRM Deal",
        {"chatwoot_conversation_id": str(conversation_id)},
        ["name", "status", "contact", "deal_owner"],
        as_dict=True,
    )


def _tiene_secuencia_activa(deal_name: str | None) -> bool:
    if not deal_name:
        return False
    return bool(frappe.db.exists("Secuencia Inscripcion", {"deal": deal_name, "estado": "Activa"}))


def _ultimo_es_humano(last_msg: dict) -> bool:
    """El último mensaje saliente lo escribió una persona, no el bot ni un
    automatismo. Reusa las marcas de `utils/autoria.py`; sin marca (mensajes
    anteriores a ese cambio) se asume humano — el sesgo correcto es no
    reactivar de más, no reactivar de menos."""
    if last_msg.get("message_type") not in (1, "outgoing"):
        return False
    ca = last_msg.get("content_attributes") or {}
    if ca.get("sofia_bot"):
        return False
    if ca.get("automatico"):
        return False
    return True


@frappe.whitelist()
def clasificar(inbox_id, dias_max: int = 30) -> dict:
    """Solo lectura. Devuelve, por cada conversación abierta y muda del inbox
    (dentro de `dias_max` días de silencio), el veredicto elegible/excluida.

    `dias_max` acota el barrido a hilos recientes — no tiene sentido reactivar
    algo de hace 3 meses; por defecto 30 días cubre de sobra el backlog actual
    (máximo medido: 17 días, 2026-09-23)."""
    convs = []
    for pagina in range(1, 41):  # 40 x 25 = 1000, techo generoso; corta antes por bandeja vacía
        lote = chatwoot_client.list_conversations(inbox_id=int(inbox_id), status="open", page=pagina)
        if not lote:
            break
        convs.extend(lote)
    ahora = datetime.now(timezone.utc)
    resultado = []
    for c in convs:
        last = c.get("last_non_activity_message") or {}
        if not last or last.get("message_type") not in (1, "outgoing"):
            continue  # no muda: el último real es del prospecto, o no hay mensajes
        ts = last.get("created_at")
        if not ts:
            continue
        cuando = datetime.fromtimestamp(ts, tz=timezone.utc)
        horas_mudo = (ahora - cuando).total_seconds() / 3600
        if horas_mudo > dias_max * 24:
            continue

        deal = _deal_de(c["id"]) or {}
        motivo = None
        if _ultimo_es_humano(last):
            motivo = "último mensaje es de un humano del equipo"
        elif _es_cliente(deal.get("contact")):
            motivo = "es cliente activo (Customer vinculado)"
        elif _tiene_secuencia_activa(deal.get("name")):
            motivo = "ya está en una Secuencia activa"
        elif deal.get("status") in ("Won", "Lost"):
            motivo = f"deal cerrado ({deal.get('status')})"

        sender = (c.get("meta") or {}).get("sender") or {}
        resultado.append({
            "conversation_id": c.get("id"),
            "nombre": sender.get("name"),
            "horas_mudo": round(horas_mudo, 1),
            "deal": deal.get("name"),
            "status": deal.get("status"),
            "elegible": motivo is None,
            "motivo": motivo,
        })

    resultado.sort(key=lambda r: -r["horas_mudo"])
    elegibles = [r for r in resultado if r["elegible"]]
    return {
        "inbox_id": int(inbox_id),
        "total_mudas": len(resultado),
        "elegibles": len(elegibles),
        "detalle": resultado,
    }


# ---------------------------------------------------------------------------
# Motor de envío (paso 6 del plan)
# ---------------------------------------------------------------------------
#
# Cron propio, NO el de `secuencias.py` — ese corre `0 12-18 L-V` con tope 4;
# barrer cada ~20 min para un paso a +3h multiplicaría el ritmo de esa
# secuencia (34 inscritos, mismo canal Baileys). Ver objeción del plan.
#
# Ventana L-V estricta HARDCODED (no es un campo por inbox): es una regla de
# negocio pareja para todos los clientes (B2B, nadie decide un lunes-viernes
# el fin de semana), no una preferencia — igual que `secuencias.py` hardcodea
# `1-5` en el crontab en vez de volverlo un campo.
DIAS_HABILES = (1, 2, 3, 4, 5)
DELAY_ENTRE_ENVIOS_SEG = (60, 120)


def _config_agente() -> dict:
    """Mismo canal HTTP que `/planeacion/generar` y `/agenda/*`: reusa
    `Chatwoot Settings.agenda_url`/`agenda_token`, no credenciales propias."""
    settings = frappe.get_single("Chatwoot Settings")
    return {
        "url": (getattr(settings, "agenda_url", None) or "").rstrip("/"),
        "token": settings.get_password("agenda_token", raise_exception=False)
        if getattr(settings, "agenda_token", None) else "",
    }


def _pedir_texto(conversation_id: int, paso: int) -> str:
    cfg = _config_agente()
    if not (cfg["url"] and cfg["token"]):
        raise RuntimeError("agente no configurado (Chatwoot Settings → agenda_url/agenda_token)")
    req = urllib.request.Request(
        f"{cfg['url']}/followup/redactar",
        data=json.dumps({"conversationId": conversation_id, "paso": paso}).encode(),
        headers={"Content-Type": "application/json", "x-sofia-token": cfg["token"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"redactor HTTP {e.code}: {e.read().decode()[:300]}")
    if not data.get("ok"):
        raise RuntimeError(f"redactor: {data.get('error')} — {data.get('mensaje')}")
    return data["texto"]


def _hhmm(cuando: datetime) -> str:
    return cuando.strftime("%H:%M")


def _dentro_de_ventana(agente, cuando: datetime) -> bool:
    if cuando.isoweekday() not in DIAS_HABILES:
        return False
    ini = agente.get("followup_ventana_inicio") or "08:00"
    fin = agente.get("followup_ventana_fin") or "18:00"
    return ini <= _hhmm(cuando) < fin


def _siguiente_hueco(agente, desde: datetime) -> datetime:
    """El primer instante >= `desde` dentro de la ventana L-V del agente.
    Mismo algoritmo que `secuencias._siguiente_hueco` (salta al inicio del
    siguiente día hábil en vez de iterar minuto a minuto sobre un fin de
    semana)."""
    ini = agente.get("followup_ventana_inicio") or "08:00"
    h_ini, m_ini = (int(x) for x in ini.split(":"))
    cand = desde
    for _ in range(14):
        if _dentro_de_ventana(agente, cand):
            return cand
        if cand.isoweekday() in DIAS_HABILES and _hhmm(cand) < ini:
            cand = cand.replace(hour=h_ini, minute=m_ini, second=0, microsecond=0)
            continue
        cand = (cand + timedelta(days=1)).replace(
            hour=h_ini, minute=m_ini, second=0, microsecond=0)
    return cand


def _delta_paso(paso_row) -> timedelta:
    valor = paso_row.get("espera_valor") or 1
    if paso_row.get("espera_unidad") == "Días":
        return timedelta(days=valor)
    return timedelta(hours=valor)


def _crear_followups_nuevos(agente, inbox_id: int) -> int:
    """Sincroniza `clasificar()` contra `Chatwoot Followup`: crea una fila por
    cada elegible que todavía no tiene ninguna (activa o cerrada) — no
    reabre una que ya salió por respuesta o se agotó."""
    pasos = agente.get("followup_pasos") or []
    if not pasos:
        return 0
    r = clasificar(inbox_id)
    creados = 0
    for h in r["detalle"]:
        if not h["elegible"]:
            continue
        if frappe.db.exists("Chatwoot Followup", {"conversation_id": h["conversation_id"]}):
            continue
        proximo = _siguiente_hueco(agente, _ahora() + _delta_paso(pasos[0]))
        frappe.get_doc({
            "doctype": "Chatwoot Followup",
            "conversation_id": h["conversation_id"],
            "inbox_id": str(inbox_id),
            "deal": h.get("deal"),
            "contacto_nombre": h.get("nombre"),
            "estado": "Activa",
            "paso_actual": 0,
            "proximo_en": proximo,
        }).insert(ignore_permissions=True)
        creados += 1
    return creados


def _ahora():
    return frappe.utils.now_datetime()


def _respondio_desde(conversation_id: int, desde) -> bool:
    """¿Llegó un mensaje del prospecto después de `desde`? Un solo GET a la
    conversación (trae `last_non_activity_message`), no una relectura del
    historial completo."""
    conv = chatwoot_client.get_conversation(conversation_id)
    last = conv.get("last_non_activity_message") or {}
    if last.get("message_type") not in (0, "incoming"):
        return False
    ts = last.get("created_at")
    if not ts:
        return False
    cuando = datetime.fromtimestamp(ts, tz=timezone.utc)
    return cuando > (desde.replace(tzinfo=timezone.utc) if desde.tzinfo is None else desde)


def _sigue_elegible(fila) -> str | None:
    """Re-chequeo al momento de enviar (no solo al inscribir): un humano pudo
    haber tomado el hilo, o el deal pudo cerrarse, desde que se creó la fila."""
    conv = chatwoot_client.get_conversation(fila.conversation_id)
    last = conv.get("last_non_activity_message") or {}
    if last and _ultimo_es_humano(last):
        return "un humano del equipo ya escribió en el hilo"
    if fila.deal:
        status = frappe.db.get_value("CRM Deal", fila.deal, "status")
        if status in ("Won", "Lost"):
            return f"el deal se cerró ({status})"
        contacto = frappe.db.get_value("CRM Deal", fila.deal, "contact")
        if _es_cliente(contacto):
            return "el contacto ya es cliente activo"
    return None


def _enviar_paso(fila, agente, pasos: list, dry_run: bool) -> None:
    paso_num = fila.paso_actual + 1  # 1-based para el redactor y el humano
    texto = _pedir_texto(fila.conversation_id, paso_num)

    if dry_run:
        chatwoot_client.create_message(
            fila.conversation_id,
            f"[DRY-RUN autofollowup, paso {paso_num}] {texto}",
            private=True,
        )
    else:
        chatwoot_client.create_message(
            fila.conversation_id, texto, private=False,
            content_attributes=autoria.marca_automatica("followup", f"paso {paso_num}"),
        )

    fila.paso_actual = paso_num
    fila.ultimo_out_at = _ahora()
    fila.texto_ultimo = texto
    if paso_num >= len(pasos):
        fila.estado = "Terminada"
    else:
        fila.proximo_en = _siguiente_hueco(agente, _ahora() + _delta_paso(pasos[paso_num]))
    fila.save(ignore_permissions=True)
    frappe.db.commit()


def _procesar_vencidos(agente, inbox_id: int, tope: int, dry_run: bool) -> dict:
    pasos = agente.get("followup_pasos") or []
    pendientes = frappe.get_all(
        "Chatwoot Followup",
        filters={"inbox_id": str(inbox_id), "estado": "Activa", "proximo_en": ["<=", _ahora()]},
        fields=["name", "conversation_id", "deal", "paso_actual", "ultimo_out_at", "creation"],
        order_by="proximo_en asc",
        limit_page_length=tope,
    )
    enviados, saltados, errores = 0, 0, 0
    for i, row in enumerate(pendientes):
        fila = frappe.get_doc("Chatwoot Followup", row.name)
        try:
            desde = fila.ultimo_out_at or fila.creation
            if _respondio_desde(fila.conversation_id, desde):
                fila.estado = "Salió por respuesta"
                fila.motivo = "el prospecto respondió"
                fila.save(ignore_permissions=True)
                frappe.db.commit()
                saltados += 1
                continue
            motivo_exclusion = _sigue_elegible(fila)
            if motivo_exclusion:
                fila.estado = "Salió a mano"
                fila.motivo = motivo_exclusion
                fila.save(ignore_permissions=True)
                frappe.db.commit()
                saltados += 1
                continue
            _enviar_paso(fila, agente, pasos, dry_run)
            enviados += 1
            if i < len(pendientes) - 1:
                time.sleep(random.uniform(*DELAY_ENTRE_ENVIOS_SEG))
        except Exception as e:
            frappe.log_error(title="autofollowup._procesar_vencidos", message=str(e))
            errores += 1
    return {"enviados": enviados, "saltados": saltados, "errores": errores}


@frappe.whitelist()
def barrer(forzar_inbox=None, dry_run: int = 0) -> dict:
    """Job del cron. Para cada `Agente IA` con `followup_activo=1`: sincroniza
    elegibles nuevos y envía los pasos vencidos (tope + delay anti-ban).

    `forzar_inbox` + `dry_run=1`: SOLO para validación manual (paso 8 del
    plan) — procesa ese inbox aunque `followup_activo` esté en 0, y escribe
    el texto como nota privada en vez de mandarlo. Nunca se llama así desde
    el cron."""
    dry_run = bool(int(dry_run))
    resultado = {}
    agentes = frappe.get_all("Agente IA", filters={"active": 1}, pluck="name")
    if forzar_inbox is not None and str(forzar_inbox) not in agentes:
        agentes = [str(forzar_inbox)]
    for inbox_id in agentes:
        agente = frappe.get_doc("Agente IA", inbox_id).as_dict()
        forzado = forzar_inbox is not None and str(forzar_inbox) == str(inbox_id)
        if not agente.get("followup_activo") and not forzado:
            continue
        ahora = _ahora()
        if not forzado and not _dentro_de_ventana(agente, ahora):
            resultado[inbox_id] = {"omitido": "fuera de ventana"}
            continue
        creados = _crear_followups_nuevos(agente, inbox_id)
        tope = agente.get("followup_max_por_corrida") or 4
        r = _procesar_vencidos(agente, inbox_id, tope, dry_run or forzado)
        resultado[inbox_id] = {"nuevos": creados, **r}
    return resultado


# ---------------------------------------------------------------------------
# Panel (paso 7 del plan) — un solo endpoint de guardado
# ---------------------------------------------------------------------------
# `frappe.client.set_value` no maneja bien un campo Table (child table); en
# vez de pelear con eso desde el front, un endpoint propio hace el guardado
# completo (escalares + tabla de pasos) en una sola llamada atómica.

_ROLES_EDICION = ("System Manager", "Sales Manager")


def _exigir_edicion():
    roles = frappe.get_roles(frappe.session.user)
    if frappe.session.user != "Administrator" and not any(r in roles for r in _ROLES_EDICION):
        frappe.throw("Sin permiso para editar la configuración de reactivación")


@frappe.whitelist()
def guardar_config(inbox_id, activo, ventana_inicio, ventana_fin, max_por_corrida, pasos) -> dict:
    """Guarda la config de autofollowup de un `Agente IA` (interruptor, ventana,
    tope y cadencia). El contenido del mensaje NO se configura aquí — lo
    redacta el agente con el contexto (decisión de Alejandro, 2026-09-24)."""
    _exigir_edicion()
    if isinstance(pasos, str):
        pasos = json.loads(pasos)
    doc = frappe.get_doc("Agente IA", inbox_id)
    doc.followup_activo = 1 if int(activo) else 0
    doc.followup_ventana_inicio = ventana_inicio
    doc.followup_ventana_fin = ventana_fin
    doc.followup_max_por_corrida = int(max_por_corrida)
    doc.set("followup_pasos", [])
    for p in pasos:
        doc.append("followup_pasos", {
            "espera_valor": int(p["espera_valor"]),
            "espera_unidad": p["espera_unidad"],
        })
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True}
