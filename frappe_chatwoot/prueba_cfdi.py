"""Caso sintético del registro de CFDI (paso 1 del plan de facturación).

    bench --site crm.lavendi.mx execute frappe_chatwoot.prueba_cfdi.main

Se fuerza a que las guardas DISPAREN. Un "0 rechazos" contra datos reales no
distingue una guarda rota de una guarda sana — por eso cada caso entra con el
dato que debe hacerla saltar. Todo lo que escribe (el caso 3b, que fuerza la
sobrefacturación) se revierte al final y se verifica que quedó revertido.

Archivo de prueba: borrable. No lo importa nada en producción.
"""

import frappe

from frappe_chatwoot.frappe_chatwoot.api.facturacion import (
    estado_cfdi, marcar_cfdi, sin_cfdi)

INV = "ACC-SINV-2026-00025"          # Félix, $59,400, con CFDI real de $29,700
OTRA = "ACC-SINV-2026-00230"         # cualquier otra factura emitida
REAL = "580cc293-91a8-4173-bdc9-842c0a584003"
FALSO = "11111111-2222-3333-4444-555555555555"


def _caso(nombre, fn, espera):
    """`espera` = 'ok' o 'rechazo'. Se compara contra lo ocurrido para que el
    resultado sea un PASA/FALLA y no una lista que hay que leer a ojo."""
    try:
        r = fn()
        ocurrio = "ok"
        detalle = r.get("accion") or r.get("estado_cfdi") if isinstance(r, dict) else r
    except Exception as e:
        ocurrio = "rechazo"
        detalle = str(e).replace("\n", " ")[:140]
    marca = "PASA" if ocurrio == espera else "FALLA"
    print(f"  [{marca}] {nombre} -> {ocurrio}: {detalle}")
    return marca == "PASA"


def main():
    antes = frappe.db.get_value("Sales Invoice", INV, "cfdi_uuid")
    print(f"estado inicial: {estado_cfdi(INV)['estado_cfdi']}\n")

    ok = [
        _caso("1 mismo UUID otra vez (idempotencia)",
              lambda: marcar_cfdi(INV, REAL, 29700, metodo_pago="PUE"), "ok"),
        _caso("2 el mismo UUID pegado a OTRA factura",
              lambda: marcar_cfdi(OTRA, REAL, 3750), "rechazo"),
        _caso("3 timbrar más que el total de la factura",
              lambda: marcar_cfdi(INV, FALSO, 40000), "rechazo"),
        _caso("3b la misma sobrefacturación con forzar=1",
              lambda: marcar_cfdi(INV, FALSO, 40000, forzar=1), "ok"),
        _caso("4 UUID con formato inválido",
              lambda: marcar_cfdi(INV, "no-es-un-uuid", 100), "rechazo"),
        _caso("5 total en cero",
              lambda: marcar_cfdi(INV, "22222222-3333-4444-5555-666666666666", 0), "rechazo"),
        _caso("6 método de pago fuera de PUE/PPD",
              lambda: marcar_cfdi(INV, "33333333-4444-5555-6666-777777777777", 100,
                                  metodo_pago="XXX"), "rechazo"),
        _caso("7 factura inexistente",
              lambda: marcar_cfdi("ACC-SINV-NOEXISTE",
                                  "44444444-5555-6666-7777-888888888888", 100), "rechazo"),
    ]

    # El caso 3b sí escribió (esa es la prueba de que `forzar` funciona). Se
    # deshace a mano: el registro es append-only por diseño, así que no existe
    # —ni debe existir— una función que borre líneas.
    mezclado = estado_cfdi(INV)
    print(f"\n  tras 3b: {len(mezclado['cfdi'])} CFDI, timbrado ${mezclado['timbrado']:,.2f} "
          f"({mezclado['estado_cfdi']})")
    frappe.db.set_value("Sales Invoice", INV, {
        "cfdi_uuid": antes, "cfdi_total": 29700, "cfdi_metodo_pago": "PUE",
        "cfdi_fecha_timbrado": "2026-09-18 12:34:19"}, update_modified=False)
    frappe.db.commit()

    limpio = estado_cfdi(INV)
    revertido = (len(limpio["cfdi"]) == 1 and limpio["cfdi"][0]["uuid"] == REAL
                 and limpio["timbrado"] == 29700.0)
    print(f"  [{'PASA' if revertido else 'FALLA'}] limpieza del caso 3b -> {limpio['estado_cfdi']}, "
          f"${limpio['timbrado']:,.2f} timbrado")
    ok.append(revertido)

    faltantes = sin_cfdi(filtro="todas", limit=500)
    print(f"\n  sin_cfdi(todas): {len(faltantes)} facturas con saldo fiscal sin timbrar")
    print(f"  Félix sigue en la lista (timbrado parcial): "
          f"{any(f['name'] == INV for f in faltantes)}")

    print(f"\n{sum(ok)}/{len(ok)} casos correctos")
