# Copyright (c) 2026, lavendi.mx
"""Página pública de pago — planes de SofÍA GPT y facturas puntuales.

`?plan=<clave>` es la URL fija que el agente entrega en el rol `sofiagpt`, en
sustitución de los 2 payment-links hospedados en la subcuenta de GHL, que
mueren con ella.

`?f=<token>` (2026-09-14) es el link corto de una factura puntual — sustituye
la URL cruda de Stripe que devolvía `stripe_pagos.link_de_pago()`
(`checkout.stripe.com/c/pay/cs_live_...`), larga y sin marca, reportada como
"parece sospechosa". El token vive en `Sales Invoice.token_pago`.

Ninguno de los dos casos cobra nada por sí mismo: cada uno crea la sesión de
Checkout del momento y manda a Stripe. El motivo de no usar un Payment Link
permanente está en `api/stripe_pagos.url_checkout_plan` — resumen: Adaptive
Pricing no se puede apagar por link, y un cobro convertido a euros entra sin
que nadie lo registre.

`/pagar?gracias=1` es a donde vuelve el cliente después de pagar; muestra el
acuse y ya. `tipo=factura` cambia el texto ("recibimos tu pago") del texto de
plan ("activamos tu cuenta") — pagar una mensualidad no es lo mismo que
contratar. El aviso al equipo no depende de que llegue aquí: lo dispara el
webhook de Stripe, que llega aunque el cliente cierre la pestaña.
"""

import frappe

from frappe_chatwoot.frappe_chatwoot.api import stripe_pagos as S

no_cache = 1


def get_context(context):
    context.no_cache = 1

    if frappe.form_dict.get("gracias"):
        context.estado = "gracias"
        context.tipo = (frappe.form_dict.get("tipo") or "").strip().lower()
        return context

    token_factura = (frappe.form_dict.get("f") or "").strip()
    if token_factura:
        try:
            frappe.local.flags.redirect_location = S.url_checkout_factura(token_factura)
            raise frappe.Redirect
        except frappe.Redirect:
            raise
        except Exception as exc:
            frappe.log_error(f"/pagar?f={token_factura}: {exc}", "stripe_pagos")
            context.estado = "error"
            return context

    plan = (frappe.form_dict.get("plan") or "").strip().lower()
    if plan not in S.PLANES_SGPT:
        # Sin plan ni token válido no se inventa uno: mandar a alguien a pagar
        # lo equivocado es peor que pedirle que vuelva por su link.
        context.estado = "plan_desconocido"
        return context

    try:
        frappe.local.flags.redirect_location = S.url_checkout_plan(plan)
        raise frappe.Redirect
    except frappe.Redirect:
        raise
    except Exception as exc:
        frappe.log_error(f"/pagar?plan={plan}: {exc}", "stripe_pagos")
        context.estado = "error"
        return context
