"""Cobro con tarjeta del nuevo Sofía — Stripe como riel de cobro de ERPNext.

Añadido por lavendi.mx; no es upstream de Frappe CRM ni de ERPNext.

POR QUÉ EXISTE
    `Auto Repeat` emite la mensualidad pero **no cobra**. Hasta hoy quien pasaba
    la tarjeta era GHL: tiene el `autoPayment` de cada plantilla recurrente y el
    link de pago de cada factura. Por eso tres recurrentes migraron PAUSADAS
    (masfortunia, EnaBarrera, esplendido) — activarlas sin resolver el cobro
    habría convertido un cargo automático en uno manual, que es un retroceso.

    Este módulo cierra ese hueco. ERPNext sigue siendo quien emite; Stripe pasa
    a ser solo el riel que cobra, en dos modos:

      · **Cargo a tarjeta guardada** (reemplaza `autoPayment` de GHL).
      · **Link de pago** (reemplaza el link nativo de la factura de GHL) — es
        como paga hoy Estrublock, que NO tiene autoPayment pero sí liquida con
        tarjeta cada mes desde el link.

    En los dos casos el webhook de Stripe registra el abono en ERPNext solo,
    con el mismo `get_payment_entry` que usa Perla. Nadie teclea el pago.

EL DATO QUE HACE ESTO POSIBLE
    Las tarjetas de los clientes ya viven en **nuestra propia cuenta de Stripe**
    (`acct_1KkOj1IZ0XKjnw5S`): GHL nunca fue el custodio, solo el que disparaba
    el cargo. Verificado el 10-sep-2026 contra los cargos reales — cada
    mensualidad es un PaymentIntent off-session sobre un `cus_…` nuestro con su
    `pm_…` guardado. Migrar el cobro NO obliga a pedirle a nadie que vuelva a
    capturar su tarjeta.

    El puente entre un `Customer` de ERPNext y su cliente de Stripe es el
    `metadata.invoiceId` que GHL le dejó al crear el cliente en Stripe: es el
    id de una factura de GHL, y ese id vive en `remarks` de la factura migrada.
    Emparejar por nombre no serviría — en ERPNext los clientes quedaron con el
    nombre del contacto ("Irene Martinez"), no el comercial ("Estrublock").

LO QUE SE PIERDE RESPECTO A GHL, Y HAY QUE DECIRLO
    Al cobrar con PaymentIntents propios (y no con Subscriptions de Stripe) no
    aplica el **Card Account Updater** de Stripe, que refresca solo las tarjetas
    renovadas o reexpedidas. Una tarjeta vencida deja de cobrar y hay que
    pedirle al cliente que la actualice. Se compensa con `tarjetas_por_vencer()`,
    que avisa antes de que pase — GHL nunca avisó: la de esplendido.mx lleva
    vencida desde 03/2026 y solo se supo al ver los cargos rechazados.

    No se usan Subscriptions a propósito: volverían a Stripe un segundo emisor
    de facturas, y tener dos sistemas emitiendo la misma mensualidad es
    exactamente el defecto del que se está saliendo.
"""

import hashlib
import hmac
import json
import secrets
import time

import frappe
import requests

from frappe_chatwoot.frappe_chatwoot.api import facturacion as F

API = "https://api.stripe.com/v1"
TIMEOUT = 30

# El modo contable con el que se registran los cobros con tarjeta. Se usa el
# master que ya existe en ERPNext en vez de crear uno nuevo: el nombre en
# inglés es la llave con la que están amarradas las cuentas de banco.
MODO_PAGO = "Credit Card"

# GHL reintentaba el cargo 3 días seguidos (verificado en los rechazos de
# esplendido.mx: 07, 08 y 09-sep). Se replica el mismo comportamiento.
DIAS_REINTENTO = 3


# ---------------------------------------------------------------------------
# Configuración y transporte
# ---------------------------------------------------------------------------

def _cfg():
    return frappe.get_cached_doc("Stripe Settings")


def _url_publica() -> str:
    """La URL a la que vuelve el cliente después de pagar.

    NO se usa `frappe.utils.get_url()`: devuelve `http://crm.lavendi.mx:8000`,
    que es el nombre interno del sitio y no resuelve desde fuera del servidor
    (se comprobó en el Checkout real: el botón "volver" apuntaba ahí). El
    dominio público del CRM es otro, y es el único que le sirve al cliente.
    """
    cfg = _cfg()
    return (cfg.url_publica or "https://sofiav2.lavendi.mx").rstrip("/")


def _clave(cfg=None) -> str:
    cfg = cfg or _cfg()
    campo = "secret_key_live" if cfg.modo == "Producción" else "secret_key_test"
    clave = cfg.get_password(campo, raise_exception=False)
    if not clave:
        frappe.throw(f"Falta la llave de Stripe ({campo}) en Stripe Settings")
    return clave


def _plano(data: dict, prefijo: str = "") -> dict:
    """Stripe recibe form-urlencoded con corchetes (`metadata[x]`), no JSON."""
    salida = {}
    for k, v in (data or {}).items():
        clave = f"{prefijo}[{k}]" if prefijo else str(k)
        if isinstance(v, dict):
            salida.update(_plano(v, clave))
        elif isinstance(v, (list, tuple)):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    salida.update(_plano(item, f"{clave}[{i}]"))
                else:
                    salida[f"{clave}[{i}]"] = item
        elif isinstance(v, bool):
            salida[clave] = "true" if v else "false"
        elif v is not None:
            salida[clave] = v
    return salida


def _api(metodo: str, ruta: str, data: dict = None, idempotencia: str = None) -> dict:
    cfg = _cfg()
    headers = {}
    if idempotencia:
        # Sin esto, un reintento de red o un doble clic cobra dos veces la misma
        # tarjeta. Stripe devuelve el mismo PaymentIntent en vez de crear otro.
        headers["Idempotency-Key"] = idempotencia
    r = requests.request(
        metodo, f"{API}{ruta}", auth=(_clave(cfg), ""),
        data=_plano(data) if data else None, headers=headers, timeout=TIMEOUT,
    )
    cuerpo = r.json() if r.content else {}
    if r.status_code >= 400:
        err = (cuerpo.get("error") or {})
        # El mensaje de Stripe es el que hay que mostrarle a quien cobra
        # ("Your card has expired"), no un 402 pelón.
        frappe.throw(err.get("message") or f"Stripe respondió {r.status_code}",
                     title=err.get("code") or "Error de Stripe")
    return cuerpo


# ---------------------------------------------------------------------------
# Vínculo Customer (ERPNext) ↔ Customer (Stripe)
# ---------------------------------------------------------------------------

def _customer_por_factura_ghl(ghl_invoice_id: str) -> str | None:
    """El id de factura de GHL que Stripe guardó en `metadata.invoiceId` es el
    mismo que la migración dejó en `remarks`. Ese es el emparejamiento exacto;
    el nombre no sirve porque en ERPNext los clientes quedaron con el nombre
    del contacto, no el de la empresa."""
    if not ghl_invoice_id:
        return None
    return frappe.db.get_value(
        "Sales Invoice", {"remarks": ["like", f"%{ghl_invoice_id}%"], "docstatus": 1},
        "customer",
    )


@frappe.whitelist()
def vincular_clientes(apply: int = 0) -> dict:
    """Recorre los clientes de Stripe y le escribe a cada `Customer` de ERPNext
    su `stripe_customer_id` y la tarjeta guardada. Idempotente."""
    F.validate_role()
    apply = frappe.utils.cint(apply)

    encontrados, sin_match, escritos = [], [], 0
    starting_after = None
    while True:
        params = {"limit": 100}
        if starting_after:
            params["starting_after"] = starting_after
        pagina = _api("GET", "/customers?" + "&".join(f"{k}={v}" for k, v in params.items()))
        for c in pagina.get("data", []):
            meta = c.get("metadata") or {}
            pm = (c.get("invoice_settings") or {}).get("default_payment_method")
            if not pm:
                continue
            customer = _customer_por_factura_ghl(meta.get("invoiceId"))
            if not customer and c.get("email"):
                customer = frappe.db.get_value(
                    "Customer", {"email_id": c["email"]}, "name"
                )
            if not customer:
                sin_match.append({"stripe": c["id"], "nombre": c.get("name"),
                                  "email": c.get("email")})
                continue
            encontrados.append({"customer": customer, "stripe": c["id"], "pm": pm})
            if apply:
                frappe.db.set_value("Customer", customer, {
                    "stripe_customer_id": c["id"],
                    "stripe_payment_method_id": pm,
                }, update_modified=False)
                escritos += 1
        if not pagina.get("has_more"):
            break
        starting_after = pagina["data"][-1]["id"]

    if apply:
        frappe.db.commit()
    return {"ok": True, "vinculados": encontrados, "sin_match": sin_match,
            "escritos": escritos}


# ---------------------------------------------------------------------------
# Estado de la tarjeta
# ---------------------------------------------------------------------------

def _tarjeta(pm_id: str) -> dict | None:
    if not pm_id:
        return None
    try:
        pm = _api("GET", f"/payment_methods/{pm_id}")
    except Exception:
        return None
    card = pm.get("card") or {}
    if not card:
        return None
    hoy = frappe.utils.getdate()
    vencida = (card["exp_year"], card["exp_month"]) < (hoy.year, hoy.month)
    return {
        "id": pm_id, "marca": card.get("brand"), "last4": card.get("last4"),
        "exp": f"{card['exp_month']:02d}/{card['exp_year']}", "vencida": vencida,
    }


@frappe.whitelist()
def estado(factura: str) -> dict:
    """Lo que necesita saber la interfaz antes de ofrecer cobrar: si Stripe está
    encendido, si ese cliente tiene tarjeta guardada y si sirve."""
    F.validate_role()
    cfg = _cfg()
    inv = frappe.db.get_value(
        "Sales Invoice", factura,
        ["customer", "customer_name", "outstanding_amount", "currency", "docstatus"],
        as_dict=True,
    )
    if not inv:
        frappe.throw("La factura no existe")

    datos = frappe.db.get_value(
        "Customer", inv.customer,
        ["stripe_customer_id", "stripe_payment_method_id", "stripe_cobro_automatico"],
        as_dict=True,
    ) or {}
    return {
        "activo": bool(cfg.enabled),
        "modo": cfg.modo,
        "saldo": float(inv.outstanding_amount or 0),
        "puede_cobrar": bool(cfg.enabled and datos.get("stripe_payment_method_id")
                             and float(inv.outstanding_amount or 0) > 0
                             and inv.docstatus == 1),
        "cobro_automatico": bool(datos.get("stripe_cobro_automatico")),
        "stripe_customer_id": datos.get("stripe_customer_id"),
        "tarjeta": _tarjeta(datos.get("stripe_payment_method_id")) if cfg.enabled else None,
    }


@frappe.whitelist()
def tarjetas_por_vencer(meses: int = 2) -> list[dict]:
    """Tarjetas guardadas que vencen dentro de N meses o ya vencieron.

    Es la compensación por no usar Subscriptions (sin Card Account Updater).
    GHL no avisaba: la de esplendido.mx venció en 03/2026 y solo se detectó
    por los cargos rechazados de septiembre, medio año después.
    """
    F.validate_role()
    hoy = frappe.utils.getdate()
    limite = frappe.utils.add_months(hoy, frappe.utils.cint(meses) or 2)
    salida = []
    for c in frappe.get_all(
        "Customer", filters={"stripe_payment_method_id": ["!=", ""]},
        fields=["name", "customer_name", "stripe_payment_method_id"], limit_page_length=0,
    ):
        t = _tarjeta(c.stripe_payment_method_id)
        if not t:
            continue
        mes, anio = t["exp"].split("/")
        fin = frappe.utils.get_last_day(f"{anio}-{mes}-01")
        if frappe.utils.getdate(fin) <= frappe.utils.getdate(limite):
            salida.append({**t, "customer": c.name, "cliente": c.customer_name})
    return sorted(salida, key=lambda x: (x["exp"][3:], x["exp"][:2]))


# ---------------------------------------------------------------------------
# Cobro
# ---------------------------------------------------------------------------

def _descripcion(factura: str, inv) -> str:
    folio = F._folio_ghl(frappe.db.get_value("Sales Invoice", factura, "remarks"),
                         frappe.db.get_value("Sales Invoice", factura, "auto_repeat"),
                         factura)
    return f"lavendi.mx — factura {folio or factura}"


def _pago_ya_registrado(factura: str, referencia: str) -> str | None:
    """Dedup fuerte por id de Stripe, por encima del dedup por monto+día de
    `registrar_pago`: el webhook puede repetirse (Stripe reintenta hasta 3 días)
    y dos abonos del mismo cargo descuadran la cartera."""
    return frappe.db.get_value(
        "Payment Entry", {"reference_no": ["like", f"%{referencia}%"], "docstatus": 1}, "name"
    )


def _aplicar_pago(factura: str, monto: float, referencia: str, fecha: str = None) -> dict:
    # Lock de la fila de la factura: NO es cosmético. Stripe manda más de un
    # evento por el mismo cobro (`checkout.session.completed` +
    # `payment_intent.succeeded`, o un reintento) y `_pago_ya_registrado` es
    # SELECT→INSERT, no atómico — dos entregas concurrentes leen "no registrado"
    # antes de que alguna inserte y quedan DOS abonos del mismo cargo. Visto el
    # 2026-09-14 en ACC-SINV-2026-00001: el mismo `pi_` cobrado dos veces
    # ($4,000 contra una factura de $2,000). `FOR UPDATE` serializa por factura
    # y se libera al commit: la segunda entrega ve el abono ya registrado (o el
    # saldo en 0 y cae por "sin saldo").
    frappe.db.sql("SELECT name FROM `tabSales Invoice` WHERE name = %s FOR UPDATE", factura)
    ya = _pago_ya_registrado(factura, referencia)
    if ya:
        return {"ok": True, "duplicado": True, "payment_entry": ya}
    return F.registrar_pago(
        factura, monto, fecha=fecha or frappe.utils.today(),
        modo=MODO_PAGO, nota=f"Stripe {referencia}",
    )


@frappe.whitelist()
def cobrar_con_tarjeta(factura: str, monto=None) -> dict:
    """Pasa la tarjeta guardada del cliente. Reemplaza el `autoPayment` de GHL.

    Es un cargo real e inmediato sobre una tarjeta ajena: por eso la interfaz
    pide confirmación y por eso el reintento lleva llave de idempotencia — un
    doble clic no puede cobrar dos veces.
    """
    F.validate_role()
    cfg = _cfg()
    if not cfg.enabled:
        frappe.throw("Stripe está apagado en Stripe Settings")

    inv = frappe.db.get_value(
        "Sales Invoice", factura,
        ["customer", "customer_name", "outstanding_amount", "currency", "docstatus"],
        as_dict=True,
    )
    if not inv:
        frappe.throw("La factura no existe")
    if inv.docstatus != 1:
        frappe.throw("La factura no está emitida")
    if (inv.currency or "MXN") != "MXN":
        frappe.throw("Solo se cobran facturas en MXN")

    saldo = round(float(inv.outstanding_amount or 0), 2)
    if saldo <= 0:
        frappe.throw("La factura no tiene saldo pendiente")
    monto = round(float(monto), 2) if monto not in (None, "") else saldo
    if monto > saldo + 0.01:
        frappe.throw(f"El monto ({monto:,.2f}) es mayor al saldo ({saldo:,.2f})")

    datos = frappe.db.get_value(
        "Customer", inv.customer, ["stripe_customer_id", "stripe_payment_method_id"],
        as_dict=True,
    ) or {}
    if not datos.get("stripe_payment_method_id"):
        frappe.throw(f"{inv.customer_name} no tiene tarjeta guardada en Stripe")

    pi = _api("POST", "/payment_intents", {
        "amount": int(round(monto * 100)),
        "currency": "mxn",
        "customer": datos["stripe_customer_id"],
        "payment_method": datos["stripe_payment_method_id"],
        # El cliente no está presente: es un cargo recurrente sobre tarjeta ya
        # autorizada, igual que lo hacía GHL.
        "off_session": True,
        "confirm": True,
        "description": _descripcion(factura, inv),
        "metadata": {"sales_invoice": factura, "cliente": inv.customer_name},
    }, idempotencia=f"si:{factura}:{monto}:{frappe.utils.today()}")

    if pi.get("status") != "succeeded":
        return {"ok": False, "estado": pi.get("status"), "payment_intent": pi.get("id"),
                "mensaje": "El cargo no se completó — revisar en Stripe"}

    # Se registra aquí y también llega por webhook; el dedup por id de Stripe
    # hace que solo cuente una vez, gane quien gane la carrera.
    res = _aplicar_pago(factura, monto, pi["id"])
    return {"ok": True, "payment_intent": pi["id"], "monto": monto, **res}


def _token_pago(factura: str) -> str:
    """Token estable del link corto de esa factura — se genera una sola vez, la
    primera vez que alguien pide un link. Las facturas que nunca lo necesitan
    no cargan con un token sin usar.

    128 bits, no derivable del nombre de la factura: con el id
    (`ACC-SINV-2026-00007`) cualquiera podría adivinar el link de otra
    factura — mismo criterio que `token_publico` de `Reunion Agendada`.

    `frappe.db.set_value` en vez de `doc.save()`: la factura está sometida
    (docstatus=1) y este campo es solo de seguimiento interno, no dato de
    negocio — no amerita pasar por el controlador completo del documento.
    """
    actual = frappe.db.get_value("Sales Invoice", factura, "token_pago")
    if actual:
        return actual
    token = secrets.token_urlsafe(12)
    frappe.db.set_value("Sales Invoice", factura, "token_pago", token, update_modified=False)
    frappe.db.commit()
    return token


@frappe.whitelist()
def link_de_pago(factura: str) -> dict:
    """Da el link corto y estable que se le manda al cliente.

    Antes esta función creaba la sesión de Checkout en el momento de generar
    el link, y el link que se copiaba era la URL cruda de Stripe
    (`checkout.stripe.com/c/pay/cs_live_...`, larga, con un token ilegible y
    que expira a las 24h aunque el cliente nunca lo abra). Reportado como
    "parece sospechoso" — con razón, no hay forma de distinguirlo a ojo de un
    link de phishing.

    Ahora el link es `{dominio}/pagar?f=<token>`, propio y corto. La sesión de
    Checkout real se crea hasta que el cliente lo abre — ver
    `url_checkout_factura()` — con el saldo recalculado en ese momento, no
    congelado desde que se generó el link.
    """
    F.validate_role()
    cfg = _cfg()
    if not cfg.enabled:
        frappe.throw("Stripe está apagado en Stripe Settings")

    inv = frappe.db.get_value(
        "Sales Invoice", factura, ["outstanding_amount", "docstatus"], as_dict=True,
    )
    if not inv:
        frappe.throw("La factura no existe")
    if inv.docstatus != 1:
        frappe.throw("La factura no está emitida")
    if round(float(inv.outstanding_amount or 0), 2) <= 0:
        frappe.throw("La factura no tiene saldo pendiente")

    token = _token_pago(factura)
    return {"ok": True, "url": f"{_url_publica()}/pagar?f={token}"}


def _checkout_session_factura(factura: str) -> str:
    """La sesión de Checkout real de una factura. Separada de `link_de_pago()`
    porque esta se llama en el momento del clic (`/pagar?f=`), no al generar
    el link — el saldo se lee fresco aquí, así que un abono parcial entre que
    se mandó el link y se abrió no deja cobrando de más."""
    inv = frappe.db.get_value(
        "Sales Invoice", factura,
        ["customer", "customer_name", "outstanding_amount", "docstatus"], as_dict=True,
    )
    if not inv or inv.docstatus != 1:
        frappe.throw("La factura no existe o no está emitida")
    saldo = round(float(inv.outstanding_amount or 0), 2)
    if saldo <= 0:
        frappe.throw("La factura ya no tiene saldo pendiente")

    token = frappe.db.get_value("Sales Invoice", factura, "token_pago")
    datos = frappe.db.get_value("Customer", inv.customer, "stripe_customer_id")
    base = _url_publica()
    cuerpo = {
        "mode": "payment",
        # Vuelve a /pagar, no a /crm/facturacion: el que paga es el cliente,
        # no el equipo — mandarlo a una pantalla del backoffice que exige
        # login del staff lo dejaba varado después de pagar.
        "success_url": f"{base}/pagar?gracias=1&tipo=factura",
        "cancel_url": f"{base}/pagar?f={token}",
        # Stripe convierte la moneda según desde dónde se abra el link
        # ("Adaptive Pricing"): en la prueba, el mismo link de $456 MXN se
        # ofreció en €24.04 por la ubicación del navegador. Se apaga a
        # propósito — el cobro tiene que ser en la moneda de la factura, o el
        # importe que devuelve el webhook no cuadra con el saldo en ERPNext.
        "adaptive_pricing": {"enabled": False},
        # Solo tarjeta. Con la lista abierta, Stripe ofrece además "Bank
        # transfer" y le da al cliente una CLABE de Stripe — una segunda vía de
        # SPEI distinta de la que el equipo comunica en la factura y en el
        # prompt de Sofía (Scotiabank a nombre de Manuel Alejandro Moreno).
        # Dos CLABEs para el mismo cobro es una fuente de confusión, no una
        # facilidad. Esto reemplaza el link de tarjeta de GHL, nada más.
        "payment_method_types": ["card"],
        "line_items": [{
            "quantity": 1,
            "price_data": {
                "currency": "mxn",
                "unit_amount": int(round(saldo * 100)),
                "product_data": {"name": _descripcion(factura, inv)},
            },
        }],
        "metadata": {"sales_invoice": factura},
        # Que el cargo del cliente quede colgado del mismo `cus_…` y no cree uno
        # nuevo: si no, la próxima vez no encontraríamos su tarjeta.
        #
        # `setup_future_usage: off_session` guarda la tarjeta para cobros
        # futuros sin el cliente presente. Es lo que convierte este link en la
        # vía para **actualizar** una tarjeta vencida: el cliente paga la
        # factura del mes y de paso queda registrada la nueva. Sin esto, el
        # cobro automático del mes siguiente seguiría intentando con la vieja.
        "payment_intent_data": {
            "metadata": {"sales_invoice": factura},
            "setup_future_usage": "off_session",
        },
    }
    if datos:
        cuerpo["customer"] = datos
    else:
        cuerpo["customer_creation"] = "always"

    ses = _api("POST", "/checkout/sessions", cuerpo)
    return ses["url"]


def url_checkout_factura(token: str) -> str:
    """Detrás de `/pagar?f=<token>` — resuelve el token a su factura y crea la
    sesión de Checkout del momento, igual que `url_checkout_plan()` para los
    planes de SofÍA GPT."""
    factura = frappe.db.get_value(
        "Sales Invoice", {"token_pago": token, "docstatus": 1}, "name"
    )
    if not factura:
        frappe.throw("Link de pago no encontrado o ya no vigente")
    return _checkout_session_factura(factura)


# ---------------------------------------------------------------------------
# Links de plan para prospectos — reemplazo de los 2 payment-links de GHL
# ---------------------------------------------------------------------------
#
# El rol `sofiagpt` del agente entrega hoy dos links hospedados en la subcuenta
# de GHL (`api.sofia.lavendi.mx/payment-link/…`, `roles/sofiagpt.md:86-87`).
# Mueren con la subcuenta, y a nadie le constaba que existían hasta el recon
# del 10-sep.
#
# Aquí NO se usa `/checkout/sessions` como en `link_de_pago()`: esa sesión
# expira (24 h) y el prompt del agente necesita una URL fija que pueda repetir
# durante meses. Los **Payment Links** de Stripe son permanentes y aceptan la
# misma configuración de cobro.
#
# POR QUÉ COBRO ÚNICO Y NO SUSCRIPCIÓN DE STRIPE
#     Una suscripción de Stripe emitiría la mensualidad por su cuenta y
#     convertiría a Stripe en un segundo emisor — el defecto del que se está
#     saliendo, y con riesgo real: el cliente acabaría con la suscripción de
#     Stripe *y* su `Auto Repeat` en ERPNext cobrándole dos veces. El link
#     cobra el **primer mes** y deja la tarjeta guardada
#     (`setup_future_usage`); de ahí en adelante el cobro mensual sale por el
#     camino que ya funciona: ERPNext emite, `cobrar_recurrentes_del_dia()`
#     pasa la tarjeta.
#
# LOS PRECIOS SON LOS VIGENTES, NO SE INVENTAN
#     $3,750 y $5,000 son los que el prompt cotiza y los que los links de GHL
#     cobran hoy en producción. El Price de "Usuarios Ilimitados" a $4,000 que
#     está en el catálogo de Stripe es un artefacto viejo que nunca se usó (los
#     links de GHL traían precio propio) — por eso se crean Prices nuevos en
#     vez de reutilizar aquél. Mismo criterio que fijó Alejandro para el cobro
#     automático: se replica GHL, no se mejora por cuenta propia.
#
# Enterprise ($9,000) queda fuera a propósito: el prompt manda agendar una
# videollamada, no cobrar por link.
#
# "in_house" (2026-09-14): el precio especial que ya traen 3 clientes viejos
# de GHL (Ena Barrera, Ena Paulina Moreno, Rafael Cardeño Oficial/medicare.mx
# — verificado contra ~18 meses de facturas migradas cada uno). En Stripe ese
# precio vivía en `prod_SQaxinv6PhPFnj`, un producto que el propio catálogo
# trae marcado **"(Obsoleto)"** desde antes de esta sesión — no se reutiliza,
# mismo criterio que con el Price de $4,000 de Usuarios Ilimitados: se crea
# limpio en vez de heredar un objeto que ya se decidió retirar.

PLANES_SGPT = {
    "starter": {"nombre": "SofÍA GPT — Plan Starter Team", "monto": 3750},
    "ilimitados": {"nombre": "SofÍA GPT — Plan Usuarios Ilimitados Pro", "monto": 5000},
    "in_house": {"nombre": "SofÍA GPT — In House (precio especial)", "monto": 2000},
}

# El acuse que ve el cliente al volver de Stripe vive en `www/pagar.html`; el
# aviso al equipo no depende de que llegue a esa página — lo dispara el webhook,
# que llega aunque cierre la pestaña.


@frappe.whitelist()
def crear_precios_planes(apply: int = 0) -> dict:
    """Crea (o encuentra) el Product + Price de cada plan en Stripe.

    Idempotente por metadata + importe: si mañana cambia el precio, no se
    reutiliza el Price viejo (cobraría de menos), se crea uno nuevo.

    `apply=0` es un dry-run a propósito: un Price en producción fija lo que se
    le cobrará a clientes reales, y eso no se hace por accidente.

    NO crea Payment Links. Se intentó y se descartó con evidencia — ver
    `url_checkout_plan()`: Adaptive Pricing no se puede apagar por link y el
    checkout salía ofreciendo euros. Los dos que se llegaron a crear quedaron
    desactivados (`plink_1UFEVs…`, `plink_1UFEVt…`).
    """
    F.validate_role()
    cfg = _cfg()
    if not cfg.enabled:
        frappe.throw("Stripe está apagado en Stripe Settings")

    salida = {}
    for clave, plan in PLANES_SGPT.items():
        existente = _price_de_plan(clave)
        if existente:
            salida[clave] = {"accion": "ya existía", "price": existente,
                             "monto": plan["monto"]}
            continue
        if not frappe.utils.cint(apply):
            salida[clave] = {"accion": "se crearía", "monto": plan["monto"],
                             "nombre": plan["nombre"]}
            continue

        producto = _api("POST", "/products", {
            "name": plan["nombre"],
            "description": "Primer mes. La mensualidad siguiente se cobra "
                           "automáticamente con la misma tarjeta.",
            "metadata": {"plan_sgpt": clave, "origen": "agente-sofia"},
        }, idempotencia=f"prod-sgpt-{clave}-{int(plan['monto'])}")
        precio = _api("POST", "/prices", {
            "currency": "mxn",
            "unit_amount": int(plan["monto"] * 100),
            "product": producto["id"],
            "metadata": {"plan_sgpt": clave},
        }, idempotencia=f"price-sgpt-{clave}-{int(plan['monto'])}")
        salida[clave] = {"accion": "creado", "price": precio["id"],
                         "producto": producto["id"], "monto": plan["monto"]}
    return {"ok": True, "apply": bool(frappe.utils.cint(apply)), "planes": salida}


def _price_de_plan(clave: str) -> str | None:
    """El Price activo del plan, buscado por su metadata."""
    datos = _api("GET", f"/prices?limit=100&active=true")
    for p in (datos.get("data") or []):
        meta = p.get("metadata") or {}
        if meta.get("plan_sgpt") == clave and p.get("currency") == "mxn" \
                and int(p.get("unit_amount") or 0) == int(PLANES_SGPT[clave]["monto"] * 100):
            return p["id"]
    return None


def url_checkout_plan(clave: str) -> str:
    """Crea una sesión de Checkout para ese plan y devuelve su URL.

    POR QUÉ EXISTE, HABIENDO PAYMENT LINKS
        Un Payment Link es permanente y sería lo natural para meterlo en el
        prompt del agente. Pero **Adaptive Pricing no se puede apagar por
        link** — es un ajuste de cuenta del Dashboard (la API rechaza el
        parámetro: "Received unknown parameter"). Se comprobó en el link recién
        creado: salía ofreciendo **€264.26** por defecto, y el webhook rechaza a
        propósito cualquier cobro que no venga en MXN, así que ese dinero
        entraría sin que nadie lo registrara.

        Las Checkout Sessions sí aceptan `adaptive_pricing.enabled=false` — es
        lo que ya hace `link_de_pago()` desde el 10-sep. Lo único que les falta
        es una URL fija, porque expiran a las 24 h. Esta función, detrás de la
        página `/pagar`, da esa URL fija: el prospecto entra siempre a la misma
        dirección y la sesión se crea en ese momento.

        Es exactamente lo que hacía GHL con `api.sofia.lavendi.mx/payment-link/
        <id>`: una URL estable propia que redirige al checkout del momento.
    """
    if clave not in PLANES_SGPT:
        frappe.throw("Plan desconocido")
    plan = PLANES_SGPT[clave]
    price = _price_de_plan(clave)
    if not price:
        frappe.throw("Ese plan todavía no tiene precio en Stripe. "
                     "Córrele `crear_precios_planes(apply=1)`.")

    base = _url_publica()
    ses = _api("POST", "/checkout/sessions", {
        "mode": "payment",
        "line_items": [{"price": price, "quantity": 1}],
        "success_url": f"{base}/pagar?gracias=1",
        "cancel_url": f"{base}/pagar?plan={clave}",
        "adaptive_pricing": {"enabled": False},
        "payment_method_types": ["card"],
        "customer_creation": "always",
        "phone_number_collection": {"enabled": True},
        "metadata": {"plan_sgpt": clave},
        "payment_intent_data": {
            "metadata": {"plan_sgpt": clave},
            "setup_future_usage": "off_session",
            "description": plan["nombre"],
        },
    })
    return ses["url"]


def _avisar_pago_de_plan(obj: dict, clave: str, monto: float, referencia: str) -> dict:
    """Le pasa al host el pago de un plan para que avise al equipo.

    No emite factura ni da de alta al cliente a propósito: el pago llega sin
    datos fiscales y emitir un CFDI con el nombre que el comprador tecleó en
    Stripe obliga a cancelarlo después. GHL tampoco facturaba solo.

    El aviso sale del host (correo + grupo interno de WhatsApp) por lo mismo
    de siempre: ni el service account de Google ni Evolution son alcanzables
    desde el contenedor de Frappe.
    """
    settings = frappe.get_single("Chatwoot Settings")
    url = getattr(settings, "onboarding_url", None)
    token = (settings.get_password("onboarding_token", raise_exception=False)
             if getattr(settings, "onboarding_token", None) else None)
    if not (url and token):
        frappe.log_error(
            f"pago de plan {clave} ({referencia}): sin onboarding_url/token, "
            "NADIE se enteró del cobro", "stripe_pagos")
        return {"ok": True, "avisado": False}

    detalle = obj.get("customer_details") or {}
    payload = {
        "plan": clave,
        "plan_nombre": PLANES_SGPT.get(clave, {}).get("nombre") or clave,
        "monto": monto,
        "nombre": detalle.get("name"),
        "email": detalle.get("email"),
        "telefono": detalle.get("phone"),
        "stripe_customer": obj.get("customer"),
        "referencia": referencia,
        "tarjeta_guardada": bool(obj.get("customer")),
    }
    import urllib.request
    req = urllib.request.Request(
        f"{url}/crm/pago-plan",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-onboarding-token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            resp.read()
        return {"ok": True, "avisado": True, "plan": clave}
    except Exception as exc:
        # Se registra pero no se devuelve error: el dinero ya entró y que
        # Stripe reintente el webhook no arregla un host caído.
        frappe.log_error(
            f"pago de plan {clave} ({referencia}): el host no recibió el aviso: {exc}",
            "stripe_pagos")
        return {"ok": True, "avisado": False, "plan": clave}


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

def _firma_valida(payload: bytes, cabecera: str, secreto: str, tolerancia: int = 300) -> bool:
    if not cabecera or not secreto:
        return False
    partes = dict(
        p.split("=", 1) for p in cabecera.split(",") if "=" in p
    )
    t, v1 = partes.get("t"), partes.get("v1")
    if not t or not v1:
        return False
    # Sin la ventana de tolerancia, un payload firmado capturado hace meses
    # seguiría siendo válido y se podría reinyectar.
    if abs(time.time() - int(t)) > tolerancia:
        return False
    esperado = hmac.new(secreto.encode(), f"{t}.".encode() + payload,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, v1)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook():
    """Punto de entrada de Stripe. Registra el abono en ERPNext solo.

    Es `allow_guest` porque Stripe no se autentica con sesión: la autenticidad
    la da la firma HMAC del cuerpo, que se verifica antes de tocar nada. Sin
    firma válida se responde 403 sin mirar el contenido.
    """
    cfg = _cfg()
    campo = "webhook_secret_live" if cfg.modo == "Producción" else "webhook_secret_test"
    secreto = cfg.get_password(campo, raise_exception=False)

    payload = frappe.request.get_data()
    cabecera = frappe.get_request_header("Stripe-Signature")
    if not _firma_valida(payload, cabecera, secreto):
        frappe.local.response["http_status_code"] = 403
        return {"ok": False, "error": "firma inválida"}

    evento = json.loads(payload)
    tipo = evento.get("type")
    obj = (evento.get("data") or {}).get("object") or {}

    if tipo == "checkout.session.completed":
        if obj.get("payment_status") != "paid":
            return {"ok": True, "ignorado": tipo}
        factura = (obj.get("metadata") or {}).get("sales_invoice")
        monto = (obj.get("amount_total") or 0) / 100.0
        referencia = obj.get("payment_intent") or obj.get("id")
        # Pago de un plan por el link que entrega el agente: no hay factura
        # contra la cual aplicarlo todavía. Se avisa y termina aquí — si
        # cayera al camino de abajo, saldría por "sin factura en metadata" y
        # el cobro no lo vería nadie.
        plan = (obj.get("metadata") or {}).get("plan_sgpt")
        if plan and not factura:
            if (obj.get("currency") or "mxn").lower() != "mxn":
                frappe.log_error(
                    f"Pago de plan {plan} en {obj.get('currency')} — revisar a mano",
                    "stripe_pagos")
            return _avisar_pago_de_plan(obj, plan, monto, referencia)
    elif tipo in ("payment_intent.succeeded", "charge.succeeded"):
        factura = (obj.get("metadata") or {}).get("sales_invoice")
        monto = (obj.get("amount_received") or obj.get("amount") or 0) / 100.0
        referencia = obj.get("payment_intent") or obj.get("id")
    else:
        return {"ok": True, "ignorado": tipo}

    if not factura or monto <= 0:
        return {"ok": True, "ignorado": "sin factura en metadata"}

    # Cinturón además del `adaptive_pricing` apagado: si por lo que sea llegara
    # un cobro en otra moneda, el importe no es comparable con el saldo en
    # pesos y registrarlo descuadraría la cartera. Mejor no registrarlo y que
    # alguien lo vea pendiente.
    if (obj.get("currency") or "mxn").lower() != "mxn":
        frappe.log_error(
            f"Cobro Stripe en {obj.get('currency')} para {factura} — no registrado",
            "stripe_pagos",
        )
        return {"ok": True, "ignorado": f"moneda {obj.get('currency')}"}

    # El webhook llega sin sesión; registrar_pago escribe asientos contables y
    # exige rol. Se corre como Administrator a propósito, después de validar la
    # firma — es el único momento en que este módulo eleva privilegios.
    frappe.set_user("Administrator")
    saldo = frappe.db.get_value("Sales Invoice", factura, "outstanding_amount")
    if saldo is None:
        return {"ok": True, "ignorado": "factura inexistente"}
    monto = min(round(float(monto), 2), round(float(saldo), 2))
    if monto <= 0:
        return {"ok": True, "ignorado": "sin saldo"}

    res = _aplicar_pago(factura, monto, referencia)
    _actualizar_tarjeta(factura, referencia)
    frappe.db.commit()
    return {"ok": True, "factura": factura, **res}


def _actualizar_tarjeta(factura: str, payment_intent: str):
    """Si el cliente pagó por link con una tarjeta distinta, se queda con la
    nueva.

    Es lo que hace que el link sirva para **renovar** una tarjeta vencida sin
    pedirle nada más al cliente: paga la factura del mes y con eso el cobro
    automático del mes siguiente ya usa la tarjeta buena. Sin esto habría que
    entrar a Stripe a cambiarla a mano, y nadie se acordaría.
    """
    if not payment_intent or not str(payment_intent).startswith("pi_"):
        return
    customer = frappe.db.get_value("Sales Invoice", factura, "customer")
    if not customer:
        return
    try:
        pi = _api("GET", f"/payment_intents/{payment_intent}")
    except Exception:
        return
    pm = pi.get("payment_method")
    cus = pi.get("customer")
    if not pm:
        return
    actual = frappe.db.get_value("Customer", customer, "stripe_payment_method_id")
    if actual == pm:
        return
    datos = {"stripe_payment_method_id": pm}
    if cus:
        datos["stripe_customer_id"] = cus
    frappe.db.set_value("Customer", customer, datos, update_modified=False)
    frappe.get_doc({
        "doctype": "Comment", "comment_type": "Comment",
        "reference_doctype": "Sales Invoice", "reference_name": factura,
        "content": f"Tarjeta actualizada desde el link de pago ({pm}).",
    }).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# Cobro automático de las mensualidades
# ---------------------------------------------------------------------------

def cobrar_recurrentes_del_dia():
    """Cobra las facturas que generó una recurrente y siguen sin pagar.

    Corre a diario. Mira los últimos `DIAS_REINTENTO` días, no solo hoy: es el
    mismo comportamiento que traía GHL, que reintentaba tres días seguidos
    antes de rendirse. Sin reintento, un rechazo transitorio del banco
    convierte la mensualidad en cobranza manual.

    Solo toca facturas *generadas por una recurrente* — nunca una factura
    suelta, aunque el cliente tenga tarjeta guardada. Cobrar sin que nadie lo
    pida es exactamente lo que no debe pasar por accidente.
    """
    cfg = _cfg()
    if not (cfg.enabled and cfg.cobro_automatico):
        return

    frappe.set_user("Administrator")
    desde = frappe.utils.add_days(frappe.utils.today(), -DIAS_REINTENTO + 1)
    candidatas = frappe.db.sql(
        """
        SELECT si.name, si.customer, si.customer_name, si.outstanding_amount
        FROM `tabSales Invoice` si
        JOIN `tabCustomer` c ON c.name = si.customer
        WHERE si.docstatus = 1
          AND si.outstanding_amount > 0.009
          AND COALESCE(si.auto_repeat, '') != ''
          AND si.posting_date BETWEEN %(desde)s AND %(hoy)s
          AND COALESCE(c.stripe_payment_method_id, '') != ''
          AND c.stripe_cobro_automatico = 1
        """,
        {"desde": desde, "hoy": frappe.utils.today()}, as_dict=True,
    )

    for inv in candidatas:
        try:
            cobrar_con_tarjeta(inv.name)
            frappe.db.commit()
        except Exception as e:
            frappe.db.rollback()
            # Queda como comentario en la propia factura, no solo en un log que
            # nadie lee: quien la vea en /cobranza tiene que enterarse de por
            # qué sigue pendiente.
            try:
                frappe.get_doc({
                    "doctype": "Comment", "comment_type": "Comment",
                    "reference_doctype": "Sales Invoice", "reference_name": inv.name,
                    "content": f"Cobro automático rechazado: {e}",
                }).insert(ignore_permissions=True)
                frappe.db.commit()
            except Exception:
                frappe.db.rollback()
            frappe.log_error(f"Cobro Stripe {inv.name}: {e}", "stripe_pagos")
