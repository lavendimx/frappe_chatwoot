"""Re-posiciona las inscripciones PVP en el paso que les toca (2026-09-15).

    bench --site crm.lavendi.mx execute frappe_chatwoot.pvp_reposicionar.main
    bench --site crm.lavendi.mx execute frappe_chatwoot.pvp_reposicionar.main --kwargs "{'apply':1}"

POR QUÉ
    La carga del 14-sep inscribió las 27 oportunidades del corte en `paso_actual=0`,
    o sea que al encender el motor las 27 recibirían el **1er Seg. PVP** — pero 18
    de ellas ya recibieron algún seguimiento antes de que el workflow pasara a
    borrador el 06-sep. Re-mandarles el 1er Seg es un duplicado que quema al
    prospecto y arriesga el número.

    La posición real se deduce del histórico rescatado de GHL
    (`migracion/data/conversaciones_msgs.jsonl`): los 5 seguimientos tienen copy
    propio e inconfundible, y se detecta cuál fue el ÚLTIMO que recibió cada
    contacto. El caso de Daniela (último = 2do Seg → se enroló en el 3er Seg)
    valida el método.

DE DÓNDE VIENEN LOS DATOS
    `pvp_paso_por_deal.json` (generado en el host por `migracion`, se copia junto a
    este módulo) trae, por deal: `ultimo_fu`, `siguiente_fu` y el `paso_actual`
    ya resuelto (índice base 0 sobre la lista de 25 pasos de la secuencia):

        siguiente seguimiento  1   2   3   4   5   (ya los recibió todos)
        paso_actual            0   6  11  16  21   23

    `paso_actual=0` para quien no tiene seguimiento detectado: deja el arranque
    natural de la secuencia (Esperar 2 días → 1er Seg), no un mensaje en frío
    inmediato.

ALCANCE
    Solo toca las inscripciones **ya existentes** de los deals del corte. No
    inscribe a nadie nuevo: los 114 deals que el corte excluyó (81 viejos, 16 con
    rechazo explícito, 18 que nunca escribieron) siguen fuera, por decisión del
    auditor del 14-sep. Tampoco enciende el motor.

IDEMPOTENTE · dry-run por defecto (`apply` obligatorio para escribir).
"""

import json
import os

import frappe

BASE = os.path.dirname(os.path.abspath(__file__))
TITULO = "4. Seguimientos — PVP"


def _cargar(nombre: str):
    with open(os.path.join(BASE, nombre), encoding="utf-8") as f:
        return json.load(f)


def main(apply=0):
    apply = bool(frappe.utils.cint(apply))
    datos = _cargar("pvp_paso_por_deal.json")["deals"]

    sec_name = frappe.db.get_value("Secuencia", {"titulo": TITULO}, "name")
    if not sec_name:
        frappe.throw(f"No existe la secuencia «{TITULO}»")
    sec = frappe.get_doc("Secuencia", sec_name).as_dict()

    from frappe_chatwoot.utils.secuencias import _siguiente_hueco
    proximo = _siguiente_hueco(sec, frappe.utils.now_datetime())

    cambios, iguales, sin_insc = [], [], []
    for deal, v in datos.items():
        if not v.get("en_corte"):
            continue
        ins = frappe.db.get_value(
            "Secuencia Inscripcion", {"secuencia": sec_name, "deal": deal},
            ["name", "paso_actual"], as_dict=True,
        )
        if not ins:
            sin_insc.append(deal)
            continue
        if ins.paso_actual == v["paso_actual"]:
            iguales.append(deal)
            continue
        cambios.append((deal, v["contacto"], ins.paso_actual, v["paso_actual"],
                        v["siguiente_fu"]))
        if apply:
            frappe.db.set_value("Secuencia Inscripcion", ins.name, {
                "paso_actual": v["paso_actual"],
                "proximo_en": proximo,
            }, update_modified=False)

    if apply:
        frappe.db.commit()

    for deal, nombre, antes, despues, sig in cambios:
        print(f"  {deal}  {str(nombre)[:26]:27} paso {antes} -> {despues}  "
              f"(siguiente = {sig}º Seg)")
    print(f"Re-posicionadas: {len(cambios)} · ya en su lugar: {len(iguales)} · "
          f"sin inscripción: {len(sin_insc)} · proximo_en={proximo} · apply={apply}")
    return {"cambiadas": len(cambios), "iguales": len(iguales),
            "sin_inscripcion": sin_insc, "proximo_en": str(proximo), "apply": apply}
