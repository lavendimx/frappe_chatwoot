"""`CRM Deal` arrastra dos vocabularios de estatus y no los mantenía nadie.

`status` es el campo de Frappe y lleva **dos cosas a la vez**: la etapa del embudo
(Lead, Cotizado, 3er Seguimiento…) y el desenlace (Won / Lost). `ghl_status` nació
en la migración de GHL (2026-09-06) con el vocabulario open/won/lost/abandoned.

El problema no es tener dos campos: es que `ghl_status` **se escribió una vez y
nadie lo volvió a tocar**. Mover una oportunidad a Ganada o Perdida desde la UI
cambia `status` y deja `ghl_status` en `open` para siempre. La divergencia crece
sola: 4 deals el 16-sep, **7 el 21-sep** ($391,200 mal clasificados).

Dónde dolía de verdad (medido el 2026-09-21):
  - `dashboard/sync_sofia.py:64` — ahí **manda `ghl_status`**, así que 5 perdidas
    entraban al pipeline abierto y 2 ganadas no se contaban como ganadas.
  - `panel.py` lo muestra en la tarjeta del contacto.
  - Las vistas guardadas del embudo ya NO dependen de él (se pasaron a `status`
    el 16-sep); los comentarios que dicen lo contrario en `fixgs.py` y
    `provisionamiento.py` quedaron desactualizados.

La cura es derivar, no sincronizar a mano: `ghl_status` pasa a ser una proyección
de `status`, recalculada en cada guardado. Se prefiere esto a retirar el campo
porque retirarlo obliga a tocar el dashboard (otro repo), `panel.py`, el diálogo
de secuencias y `dashboard_readonly.py` — mismo resultado, cuatro veces el riesgo.
Retirarlo sigue siendo posible después, ya sin que el campo mienta mientras tanto.

Corre en `validate` de un doctype que se guarda todo el tiempo: es comparación de
strings, sin I/O ni commit, y **nunca lanza** — si algo falla, `ghl_status` se
queda como estaba en vez de tumbar el guardado de la oportunidad.
"""

import frappe

# `abandoned` era un estado propio de GHL que Frappe no modela: su `status` cae en
# Lost igual que un perdido normal. Se preserva si ya estaba (no hay forma de
# volver a deducirlo desde `status`, y `secuencias.ESTADOS_QUE_SACAN` lo reconoce),
# pero ningún deal nuevo puede nacer así — GHL está retirado desde 2026-09-11.
_CERRADOS = {"Won": "won", "Lost": "lost"}


def derivar(status, ghl_status_actual=None):
    """Proyección pura: qué `ghl_status` le corresponde a este `status`."""
    destino = _CERRADOS.get(status, "open")
    if destino == "lost" and ghl_status_actual == "abandoned":
        return "abandoned"
    return destino


def sincronizar(doc, method=None):
    try:
        esperado = derivar(doc.get("status"), doc.get("ghl_status"))
        if doc.get("ghl_status") != esperado:
            doc.ghl_status = esperado
    except Exception:
        frappe.log_error(frappe.get_traceback(), "estatus_deal.sincronizar")


def reparar_todos(aplicar=False):
    """Backfill. Por defecto simula. Escribe con `update_modified=False` para no
    disparar `on_update` en 3,7xx deals (onboarding + nutrición): el 18-sep un
    backfill sin esa bandera generó 251 avalanchas de ToDo y notificaciones."""
    desalineados = frappe.db.sql(
        """SELECT name, status, ghl_status, deal_value FROM `tabCRM Deal`""", as_dict=True
    )
    cambios = []
    for d in desalineados:
        esperado = derivar(d.status, d.ghl_status)
        if (d.ghl_status or "") != esperado:
            cambios.append({**d, "nuevo": esperado})

    if aplicar:
        for c in cambios:
            frappe.db.set_value(
                "CRM Deal", c["name"], "ghl_status", c["nuevo"], update_modified=False
            )
        frappe.db.commit()

    print(frappe.as_json({"aplicado": aplicar, "cambios": len(cambios), "detalle": cambios[:20]}))
    return cambios
