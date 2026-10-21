import frappe
from frappe.utils import flt, now_datetime

PRINT_FORMAT = "Cotización Estrublock"


def _pdf(name):
    """Genera el PDF de la cotización y lo guarda como File público."""
    doc = frappe.get_doc("Cotizacion", name)
    import re
    from frappe.utils.pdf import get_pdf
    html = frappe.get_print("Cotizacion", name, print_format=PRINT_FORMAT)
    base = f"https://{frappe.local.site}"
    html = re.sub(r'https?://[^"\'\s>]*:8000', base, html)
    html = html.replace('href="/assets/', 'href="%s/assets/' % base).replace('src="/assets/', 'src="%s/assets/' % base)
    html = re.sub(r'<link[^>]*>', '', html)
    html = re.sub(r'<div class="action-banner print-hide">.*?</div>\s*', '', html, flags=re.S)
    content = get_pdf(html)
    fname = f"{name}.pdf"
    existing = frappe.get_all("File", filters={"attached_to_doctype": "Cotizacion", "attached_to_name": name, "file_name": fname}, fields=["name"], limit=1)
    if existing:
        frappe.delete_doc("File", existing[0]["name"], ignore_permissions=True, force=True)
    f = frappe.get_doc({
        "doctype": "File", "file_name": fname, "is_private": 0,
        "content": content, "attached_to_doctype": "Cotizacion", "attached_to_name": name,
    })
    f.save(ignore_permissions=True)
    doc.db_set("pdf", f.file_url, update_modified=False)
    return f.file_url


@frappe.whitelist()
def crear_desde_oportunidad(oportunidad, generar_pdf=1):
    """Crea un borrador de cotización a partir de los productos de una oportunidad."""
    deal = frappe.get_doc("CRM Deal", oportunidad)
    items = []
    for p in (deal.get("products") or []):
        prod = frappe.get_doc("CRM Product", p.product_code) if p.get("product_code") else None
        items.append({
            "producto": p.get("product_code"),
            "codigo": prod.product_code if prod else p.get("product_code"),
            "descripcion": prod.product_name if prod else p.get("product_name"),
            "medida": (prod.get("medida") if prod else "") or "",
            "unidad": (prod.get("unidad_venta") if prod else "pieza") or "pieza",
            "piezas_por_m2": flt(prod.get("piezas_por_m2")) if prod else 0,
            "cantidad": flt(p.get("qty")),
            "precio_unitario": flt(p.get("rate")) or (flt(prod.standard_rate) if prod else 0),
            "descuento_pct": flt(p.get("discount_percentage")),
        })
    if not items:
        frappe.throw("La oportunidad no tiene productos. Agrega al menos uno antes de cotizar.")

    cliente = deal.get("ghl_opp_name") or deal.get("deal_name") or deal.get("lead_name") or "Cliente"
    doc = frappe.get_doc({
        "doctype": "Cotizacion",
        "cliente_nombre": cliente,
        "contacto": deal.get("contact"),
        "oportunidad": deal.name,
        "asesora": frappe.session.user,
        "items": items,
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    url = _pdf(doc.name) if int(generar_pdf or 0) else None
    return {"name": doc.name, "pdf_url": url, "estado": doc.estado}


@frappe.whitelist()
def enviar(name, canal="email", mensaje=None):
    """Envía la cotización por correo o WhatsApp desde la conversación ligada."""
    doc = frappe.get_doc("Cotizacion", name)
    pdf_url = doc.pdf or _pdf(name)
    site = frappe.utils.get_url()
    link = f"{site}{pdf_url}"
    mensaje = mensaje or f"Hola, te comparto la cotización {doc.name}. Quedo atenta a cualquier duda."

    if canal == "email":
        email = frappe.db.get_value("Contact", doc.contacto, "email_id") if doc.contacto else None
        if not email:
            frappe.throw("El contacto no tiene correo registrado.")
        frappe.sendmail(recipients=[email], subject=f"Cotización {doc.name}", message=mensaje,
                        attachments=[{"fname": f"{doc.name}.pdf", "fcontent": open(frappe.get_site_path("public", pdf_url.lstrip("/")), "rb").read()}])
        doc.db_set({"estado": "Enviada", "enviado_el": now_datetime(), "enviado_por": frappe.session.user, "enviado_a": email})
    elif canal == "whatsapp":
        conv = frappe.db.get_value("CRM Deal", doc.oportunidad, "chatwoot_conversation_id") if doc.oportunidad else None
        if not conv:
            frappe.throw("La oportunidad no tiene conversación de Chatwoot ligada.")
        from frappe_chatwoot.api.chatwoot import send_message
        send_message(conversation_id=int(conv), content=mensaje + " " + link)
        doc.db_set({"estado": "Enviada", "enviado_el": now_datetime(), "enviado_por": frappe.session.user, "enviado_a": f"chatwoot:{conv}"})
    else:
        frappe.throw("Canal inválido.")

    frappe.db.commit()
    return {"ok": True, "pdf_url": pdf_url}
