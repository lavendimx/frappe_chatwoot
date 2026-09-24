"""Auto-expira las pausas humanas de `Chatwoot Pausa`.

POR QUÉ EXISTE
    Hasta hoy `Chatwoot Pausa` era indefinida: solo un humano la levantaba a
    mano con el botón "Reanudar agente" (ver api/agentes.py::resume_conversation).
    Si nadie lo hacía, el agente se quedaba callado para siempre en esa
    conversación. Este job la expira sola pasados los minutos configurados
    por inbox en `Agente IA.minutos_pausa_humana` (default 90), contados
    desde `Chatwoot Pausa.paused_at`.

    `Agente IA.pausar_ante_intervencion_humana` es un interruptor DISTINTO:
    lo lee agente-ia (host, Node) para decidir si pausar automáticamente al
    ver un saliente humano. No cambia el comportamiento de este job — una
    pausa que ya existe se expira igual, la haya creado ese mecanismo o el
    botón manual "Pausar agente" (api/agentes.py::pause_conversation).

INTERRUPTOR GLOBAL — POR QUÉ NACE APAGADO
    `Chatwoot Settings.expirar_pausas_activo` (Custom Field, default 0).
    Registrar el job en el scheduler NO enciende nada — mismo patrón que
    `secuencias_activas`/`aviso_facturas_vencidas_activo`/etc.

    Es necesario aquí más que en ningún otro switch de esta app: al momento
    de escribir esto hay ~20 `Chatwoot Pausa` activas en inbox 5, varias de
    hace días o semanas (algunas con una conversación entregada a mano a un
    humano — ver bitácora, caso Frida Ortega conv 523). Si el job corriera
    con el switch encendido desde el primer despliegue, TODAS resucitarían
    al agente de golpe en la primera corrida — un cambio de comportamiento
    brusco en conversaciones reales sin que nadie lo pida. Encenderlo es
    decisión de Alejandro, después de revisar esas pausas existentes.

CÓMO SE MARCA LA REANUDACIÓN
    Se reusa el mecanismo de la reanudación manual
    (api/agentes.py::_marcar_reanudacion / doctype `Chatwoot Reanudacion`):
    sin esto el bot seguiría respetando el silencio-humano de un mensaje que
    ya quedó viejo, y tardaría de más en retomar la conversación aunque el
    cliente ya hubiera vuelto a escribir. Ver el docstring de
    `_marcar_reanudacion` para el porqué del campo `reanudado_ms`.
"""

import frappe

from frappe_chatwoot.frappe_chatwoot.api.agentes import _marcar_reanudacion

MINUTOS_DEFAULT = 90


def _activo() -> bool:
    return bool(frappe.db.get_single_value("Chatwoot Settings", "expirar_pausas_activo"))


def _minutos_por_inbox() -> dict:
    rows = frappe.get_all("Agente IA", fields=["name", "minutos_pausa_humana"])
    return {
        row.name: frappe.utils.cint(row.minutos_pausa_humana) or MINUTOS_DEFAULT
        for row in rows
    }


def expirar_pausas() -> dict:
    """Scheduled job (`*/5 * * * *`). Sale en la primera línea si el
    interruptor global está apagado o si no hay pausas activas — barato en
    el caso común."""
    if not _activo():
        return {"activo": False, "expiradas": 0}

    pausas = frappe.get_all(
        "Chatwoot Pausa",
        fields=["name", "conversation_id", "inbox_id", "paused_at"],
    )
    if not pausas:
        return {"activo": True, "expiradas": 0}

    minutos_por_inbox = _minutos_por_inbox()
    ahora = frappe.utils.now_datetime()
    expiradas = []

    for pausa in pausas:
        if not pausa.paused_at:
            # Pausa sin fecha (conversion vieja / anómala): no sabemos su
            # edad, no se auto-expira — sigue requiriendo el botón manual.
            continue
        minutos = minutos_por_inbox.get(str(pausa.inbox_id), MINUTOS_DEFAULT)
        limite = frappe.utils.add_to_date(pausa.paused_at, minutes=minutos)
        if ahora < limite:
            continue
        frappe.delete_doc("Chatwoot Pausa", pausa.name, ignore_permissions=True)
        _marcar_reanudacion(frappe.utils.cint(pausa.conversation_id))
        expiradas.append(pausa.conversation_id)

    if expiradas:
        frappe.db.commit()

    return {"activo": True, "expiradas": len(expiradas), "conversaciones": expiradas}
