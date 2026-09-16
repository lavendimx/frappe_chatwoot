"""Carga la rama PVP del motor de secuencias (2026-09-14).

    bench --site crm.lavendi.mx execute frappe_chatwoot.secuencias_cargar_pvp.main
    bench --site crm.lavendi.mx execute frappe_chatwoot.secuencias_cargar_pvp.main --kwargs "{'apply':1}"

QUÉ HACE
    Deja en producción, **apagado**, el equivalente de la rama PVP del workflow
    «4. Seguimientos» de GHL:
      1. Una `Secuencia` ("4. Seguimientos — PVP", ventana 12:00–18:00 L-V,
         `max_por_corrida=4`, `parar_si_responde=1`).
      2. Sus 25 `Secuencia Paso` en orden (9 esperas, 5 WhatsApp, 5 email,
         4 cambios de etapa, 2 tareas).
      3. Las inscripciones de los deals seleccionados por
         `patches/seleccionar_deals_pvp.py` (último mensaje entrante ≤90 días,
         sin rechazo explícito).

POR QUÉ LOS DATOS VIENEN EN JSON Y NO SE PARSEAN AQUÍ
    El generador (`workflows-ghl/generar_pasos_pvp.py`) corre en el host, donde
    viven el JSON crudo del workflow, `snippets.json` y `media-map.json`. El
    loader que corre dentro del contenedor solo recibe la lista ya resuelta — así
    el parseo frágil se verifica aparte, sin tocar la base.

IDEMPOTENTE
    `Secuencia` se busca por `titulo`; los pasos se reconstruyen (no se duplican);
    una inscripción existente del mismo (secuencia, deal) se respeta.
    Dry-run por defecto: `apply` es obligatorio para escribir.
"""

import json
import os

import frappe

BASE = os.path.dirname(os.path.abspath(__file__))
TITULO = "4. Seguimientos — PVP"
WF_ID = "23044b94-00a7-486d-903a-764474d70d55"


def _cargar(nombre: str):
    with open(os.path.join(BASE, nombre), encoding="utf-8") as f:
        return json.load(f)


def main(apply=0):
    apply = bool(frappe.utils.cint(apply))
    pasos = _cargar("secuencias_pvp_pasos.json")
    deals = _cargar("secuencias_pvp_deals.json")["deals"]

    # --- 1. La secuencia -----------------------------------------------------
    campos = {
        "titulo": TITULO,
        "activa": 0,                 # a propósito: nada sale hasta encenderlo
        "disparador": "Manual",
        "producto": "PVP",
        "inbox_id": "5",             # WhatsApp de lavendi.mx (Evolution)
        "parar_si_responde": 1,
        "permitir_reingreso": 0,
        "horario_habil": 1,
        "ventana_inicio": "12:00",
        "ventana_fin": "18:00",
        "ventana_dias": "1,2,3,4,5",
        "max_por_corrida": 4,
        "ghl_workflow_id": WF_ID,
    }
    existente = frappe.db.exists("Secuencia", {"titulo": TITULO})
    if existente:
        sec = frappe.get_doc("Secuencia", existente)
        sec.update(campos)
        sec.pasos = []
    else:
        sec = frappe.new_doc("Secuencia")
        sec.update(campos)
    for p in pasos:
        sec.append("pasos", p)
    print(f"Secuencia: {'actualizada' if existente else 'nueva'} · "
          f"{len(pasos)} pasos · activa={sec.activa}")
    if apply:
        if existente:
            sec.save(ignore_permissions=True)
        else:
            sec.insert(ignore_permissions=True)

    # --- 2. Inscripciones ----------------------------------------------------
    from frappe_chatwoot.utils.secuencias import _siguiente_hueco
    cuando = _siguiente_hueco(dict(campos), frappe.utils.now_datetime())

    nuevas, ya = 0, 0
    for dn in deals:
        if frappe.db.exists("Secuencia Inscripcion", {"secuencia": sec.name or TITULO, "deal": dn}):
            ya += 1
            continue
        d = frappe.db.get_value("CRM Deal", dn, ["contact", "chatwoot_conversation_id"],
                                as_dict=True) or {}
        if apply:
            frappe.get_doc({
                "doctype": "Secuencia Inscripcion",
                "secuencia": sec.name,
                "deal": dn,
                "contacto": d.get("contact"),
                "conversation_id": d.get("chatwoot_conversation_id") or "",
                "estado": "Activa",
                "paso_actual": 0,
                "proximo_en": cuando,
                "ultimo_envio_at": cuando,
            }).insert(ignore_permissions=True)
        nuevas += 1

    if apply:
        frappe.db.commit()
    print(f"Inscripciones: {nuevas} nuevas · {ya} ya existían · "
          f"proximo_en={cuando} · apply={apply}")
    return {"secuencia": sec.name if apply else TITULO, "pasos": len(pasos),
            "inscritos": nuevas, "ya": ya, "apply": apply}
