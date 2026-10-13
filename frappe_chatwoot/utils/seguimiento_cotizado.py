# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Disparo de la Secuencia de seguimiento al mover un `CRM Deal` a "Cotizado".

CONTEXTO
    En GHL, cuando un vendedor mandaba la cotización (PDF) y movía la oportunidad a la
    etapa "Cotizado", eso disparaba la secuencia de seguimientos. En Sofía CRM esa pieza
    hoy solo existe del lado del AGENTE DE IA (`agente-ia/server.js::programarSeguimientoCotizado`):
    detecta el PDF en el chat y, si lo detecta, mueve el Deal e inscribe en la Secuencia.
    El problema: esa detección corre solo mientras la conversación NO está pausada por un
    humano — en cuanto un vendedor toma la conversación y cotiza él mismo, el procesamiento
    automático se salta por completo y el deal se queda sin pasar a "Cotizado" ni a la
    secuencia (caso real: Alejandro con Lorena Zaragoza, conv 2300 de Chatwoot).

    Este módulo mueve el disparador a donde siempre debió estar: el propio cambio de etapa
    del Deal en el CRM. Si el vendedor mueve el Deal a "Cotizado" —algo que ya hace de todos
    modos, sea que haya cotizado por WhatsApp o por cualquier otro canal— eso por sí solo
    inscribe el deal en la secuencia. Ya no depende de que el bot haya visto el PDF.

GATE POR SITIO — obligatorio, sin default
    Esta app está instalada en varios sitios de cliente y cada uno tiene su propio embudo
    con sus propios nombres de etapa. Estrublock, en particular, YA TIENE una etapa llamada
    "Cotizado" en su propio pipeline de 21 etapas que no tiene nada que ver con esta
    secuencia de lavendi.mx — si este hook actuara ahí sin gate, le inventaría una etapa y
    un `inscribir_deal` contra una secuencia que no existe en ese sitio sería, en el mejor
    caso, un error silencioso y, en el peor, contaminación cruzada de embudos ajenos.

    Por eso el hook exige una clave explícita en `site_config.json` de CADA sitio —mismo
    criterio que `utils/seat_cap.py` (`sofia_lite_max_usuarios`) y que `SEGUIMIENTO_COTIZADO`
    en `agente-ia/.env`—: sin la clave, declarada y completa, no hace NADA. Nunca hay un
    valor por default, porque un default sería, por definición, el embudo de lavendi.mx
    corriendo en el CRM de otro cliente.

        "seguimiento_cotizado": {
            "estado_cotizado": "Cotizado",
            "secuencia": "4. Seguimientos — PVP",
            "estados_previos": ["Lead", "Contactado", "Oportunidades futuras"]
        }

    Los 3 campos son obligatorios; una entrada incompleta (falta alguno, o
    `estados_previos` vacío) se trata exactamente igual que ausente — no corre.

    `estado_cotizado`/`estados_previos` verificados contra el catálogo real de
    `crm.lavendi.mx` (`fixtures/catalogos/crm_deal_status.json`): "Lead" (Open, posición 2),
    "Contactado" (Ongoing, posición 3) y "Oportunidades futuras" (Open, posición 1) son las
    3 etapas que preceden a "Cotizado" (Ongoing, posición 4) — los mismos 3 nombres que ya
    usa `SEGUIMIENTO_COTIZADO["5"]` en `agente-ia/.env` para este mismo sitio.

TRANSICIÓN NUEVA, NO REGRESAR ETAPAS
    Mismo patrón que `onboarding.on_deal_update` / `nutricion.on_deal_update`: se compara
    contra `doc.get_doc_before_save()` para actuar solo cuando la etapa ACABA de cambiar a
    la de cotización, no en cada re-guardado posterior del deal que ya estaba ahí (Frappe
    dispara `on_update` varias veces por guardado).

    Guard extra, igual al de `agente-ia/server.js` ("Guard: si ya está cotizado o más
    adelante, no se toca nada"): si el deal viene de una etapa que NO está en
    `estados_previos` (p. ej. venía de "3er Seguimiento", o de Won/Lost reabierto), tampoco
    se actúa — moverlo hacia atrás en el embudo o reinscribirlo en una secuencia que ya
    corrió sería peor que no tocar nada.

PERMISOS — por qué se eleva a Administrator
    `inscribir_deal` (`utils/secuencias.py`) exige rol `System Manager`/`Sales Manager`
    (`_exigir_edicion`) porque fue pensado para la pantalla manual de Secuencias, operada
    por el equipo. Pero el disparador real de este hook es CUALQUIER vendedor con permiso
    de mover su propio Deal — típicamente un `Sales User`, que no tiene esos roles.

    El job en background hereda el usuario de la sesión que guardó el documento:
    `frappe.enqueue` captura `frappe.session.user` al momento de encolar (ver
    `frappe.utils.background_jobs.enqueue`), así que sin elevar privilegios aquí el
    vendedor normal se llevaría un `PermissionError` al mover su propia oportunidad —
    justo lo que el hook vino a evitar. Se eleva a `Administrator` antes de llamar, mismo
    patrón ya usado en este proyecto para lo mismo
    (`api/stripe_pagos.py::cobrar_recurrentes_del_dia`/`registrar_pago`).

IDEMPOTENCIA
    `inscribir_deal` ya la resuelve: si el deal tiene una inscripción `Activa` en esa
    secuencia, lanza `frappe.ValidationError("Esa oportunidad ya está inscrita...")`, que
    aquí se trata como éxito (no hay nada que hacer, no es una falla). Si tenía una
    inscripción vieja (`Terminada`/`Salió`), la reactiva en vez de duplicar fila.
"""

import frappe


def _config() -> dict | None:
    """La config de seguimiento-por-cotización de ESTE sitio, o `None` si no está
    declarada o viene incompleta. Ver GATE POR SITIO en el docstring del módulo."""
    cfg = frappe.conf.get("seguimiento_cotizado")
    if not isinstance(cfg, dict):
        return None

    estado_cotizado = str(cfg.get("estado_cotizado") or "").strip()
    secuencia = str(cfg.get("secuencia") or "").strip()
    estados_previos = [
        str(s).strip() for s in (cfg.get("estados_previos") or []) if str(s).strip()
    ]
    if not (estado_cotizado and secuencia and estados_previos):
        return None

    return {
        "estado_cotizado": estado_cotizado,
        "secuencia": secuencia,
        "estados_previos": estados_previos,
    }


def on_deal_update(doc, method=None):
    """Hook `CRM Deal.on_update`. Sale en la primera condición para cualquier guardado
    que no sea una transición NUEVA a la etapa de cotización declarada por este sitio —
    mismo criterio que `onboarding.on_deal_update`/`nutricion.on_deal_update` para no
    encarecer el guardado normal de un deal."""
    cfg = _config()
    if not cfg:
        return
    if doc.status != cfg["estado_cotizado"]:
        return

    anterior = doc.get_doc_before_save()
    if anterior is not None:
        if anterior.status == cfg["estado_cotizado"]:
            return  # re-guardado ya en esa etapa: no reinscribir en cada save
        if anterior.status not in cfg["estados_previos"]:
            return  # viene de una etapa no declarada como previa: no regresar/reinscribir

    frappe.enqueue(
        "frappe_chatwoot.utils.seguimiento_cotizado.ejecutar",
        queue="short",
        timeout=60,
        deal_name=doc.name,
        secuencia=cfg["secuencia"],
        enqueue_after_commit=True,
    )


def ejecutar(deal_name: str, secuencia: str) -> dict:
    """Corre en background. Eleva a Administrator — ver PERMISOS en el docstring del
    módulo: el vendedor que movió el Deal normalmente no tiene el rol que
    `inscribir_deal` exige para operar una secuencia a mano."""
    from .secuencias import inscribir_deal

    frappe.set_user("Administrator")
    try:
        resultado = inscribir_deal(secuencia=secuencia, deal=deal_name)
        frappe.db.commit()
        return {"ok": True, **resultado}
    except frappe.ValidationError as exc:
        if "ya está inscrita" in str(exc):
            # Idempotencia: ver IDEMPOTENCIA en el docstring del módulo — no es una falla.
            return {"ok": True, "deal": deal_name, "secuencia": secuencia, "ya_inscrita": True}
        frappe.log_error(
            f"seguimiento_cotizado {deal_name} -> {secuencia}: {exc}",
            "Seguimiento cotizado",
        )
        return {"ok": False, "deal": deal_name, "secuencia": secuencia, "motivo": str(exc)}
