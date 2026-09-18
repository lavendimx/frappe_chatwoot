"""Los CRM Lead nacidos en Chatwoot entran al embudo; los migrados de GHL no.

Contexto (medido 2026-09-18): de 603 `CRM Lead` abiertos, **581 vienen de la
migración de GHL y allá nunca fueron oportunidad** — su información ya vive en
`Contact` y solo 2 de 581 tienen su contacto ligado a algún Deal. Convertirlos
inflaría el embudo con gente que nadie trabaja, así que NO se tocan aquí.

Los otros 30 nacieron de una conversación real de WhatsApp por la vía vieja
(antes del 2026-09-18, cuando el agente pasó a crear `CRM Deal` directo). Esos
sí son prospectos y quedaron fuera del embudo y de las secuencias — el caso
Jorge Fonk. Este script los reparte en tres lotes:

  A  descarte  -> pruebas de QA y el número propio de la agencia. Se MARCAN
                  (`Junk` + converted=1), no se borran: salen de la vista igual
                  y el rastro queda, incluidas sus conversaciones de Chatwoot.
  B  ya tienen -> su teléfono ya está en un Deal. Se vincula la conversación al
     Deal          Deal si le falta y el Lead se cierra como `Converted`. NO se
                  crea un segundo Deal del mismo cliente.
  C  convertir -> `convert_to_deal` nativo, pasándole el `contact` que el lead
                  ya tiene, para que no nazca una ficha duplicada.

Idempotente: los lotes se recalculan sobre `converted=0`, así que una segunda
corrida no encuentra nada que hacer.
"""

import frappe

PRUEBA = ("prueba", "qa ", "test", "borrar", "tmp")
# WhatsApp de la agencia. Aparece como "lead" porque alguien escribió desde él.
TEL_PROPIO = "5559668622"
OWNER = "alejandro.moreno@lavendi.mx"


def _es_prueba(nombre):
    n = (nombre or "").lower()
    return any(p in n for p in PRUEBA)


def _digitos(v):
    return "".join(c for c in (v or "") if c.isdigit())[-10:]


def _clasificar():
    leads = frappe.get_all(
        "CRM Lead",
        filters={"converted": 0, "chatwoot_conversation_id": ["!=", ""]},
        fields=["name", "lead_name", "mobile_no", "contact", "organization",
                "chatwoot_conversation_id", "chatwoot_inbox_id"],
        order_by="creation",
    )
    a, b, c = [], [], []
    for l in leads:
        tel = _digitos(l.mobile_no)
        if _es_prueba(l.lead_name) or tel == TEL_PROPIO:
            a.append(l)
            continue
        deal = None
        if len(tel) == 10:
            r = frappe.db.sql(
                """select name, ifnull(chatwoot_conversation_id,'') cw from `tabCRM Deal`
                   where right(regexp_replace(ifnull(mobile_no,''),'[^0-9]',''),10)=%s
                   order by creation limit 1""", tel, as_dict=1)
            if r:
                deal = r[0]
        if deal:
            l["deal"] = deal.name
            l["deal_cw"] = deal.cw
            b.append(l)
        else:
            c.append(l)
    return a, b, c


def ejecutar(solo=None, aplicar=0):
    """`solo`: nombre de un CRM Lead para procesar uno y verificar antes del resto."""
    aplicar = int(aplicar)
    a, b, c = _clasificar()
    if solo:
        a = [x for x in a if x.name == solo]
        b = [x for x in b if x.name == solo]
        c = [x for x in c if x.name == solo]
    print("modo:", "APLICAR" if aplicar else "SIMULACION", "| A:", len(a), "B:", len(b), "C:", len(c))

    for l in a:
        print("  [A descarte]", l.name, "|", l.lead_name)
        if aplicar:
            frappe.db.set_value("CRM Lead", l.name, {"status": "Junk", "converted": 1},
                                update_modified=False)

    for l in b:
        print("  [B ya tiene Deal]", l.name, "->", l.deal,
              "| vincula conv" if not l.deal_cw else "| deal ya tiene conv")
        if aplicar:
            if not l.deal_cw:
                frappe.db.set_value("CRM Deal", l.deal, {
                    "chatwoot_conversation_id": l.chatwoot_conversation_id,
                    "chatwoot_inbox_id": l.chatwoot_inbox_id,
                }, update_modified=False)
            frappe.db.set_value("CRM Lead", l.name, {"status": "Converted", "converted": 1},
                                update_modified=False)

    for l in c:
        print("  [C convertir]", l.name, "|", (l.lead_name or "")[:24], "| contacto:", l.contact)
        if aplicar:
            from crm.fcrm.doctype.crm_lead.crm_lead import convert_to_deal
            doc = frappe.get_doc("CRM Lead", l.name)
            doc.flags.ignore_permissions = True
            deal = convert_to_deal(lead=l.name, doc=doc, existing_contact=l.contact)
            # `convert_to_deal` no garantiza estos tres: `ghl_status` es el campo
            # por el que filtran las vistas guardadas (sin él el deal no sale en
            # "Solo activas"), y la conversación es el vínculo con la bandeja.
            frappe.db.set_value("CRM Deal", deal, {
                "ghl_status": "open",
                "chatwoot_conversation_id": l.chatwoot_conversation_id,
                "chatwoot_inbox_id": l.chatwoot_inbox_id,
                "deal_owner": OWNER,
            }, update_modified=False)
            print("       ->", deal)
    if aplicar:
        frappe.db.commit()
    print("listo.")
