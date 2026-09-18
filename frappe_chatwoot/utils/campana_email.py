# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Job de campañas de email programadas.

Se copia a
`apps/frappe_chatwoot/frappe_chatwoot/frappe_chatwoot/utils/campana_email.py`.

Plan: `planes/campanas-contenedor-y-cadencia.md` (paso 5).

POR QUÉ SE DETIENE EN VEZ DE SALTAR
    Si el paso que toca no tiene `newsletter` ligado (nadie lo ha redactado
    todavía), el job **no sigue con el siguiente** — se detiene ahí y avisa.
    Saltarlo rompería la cadencia declarada (el paso 4 llegaría antes de
    tiempo si el 3 se saltó) y mandaría la serie desordenada. Es la misma
    lógica de `_debe_salir` en `utils/secuencias.py`: preferir no enviar a
    enviar mal.

DOS INTERRUPTORES, A PROPÓSITO (mismo patrón que `secuencias_activas`)
    `Chatwoot Settings.campanas_automaticas` es el freno global (nace en 0);
    `Campana Email.activa` es por campaña. Ambos deben estar encendidos para
    que una campaña mande algo sola. Registrar el job en el scheduler NO
    enciende nada por sí solo.
"""

import frappe


def avanzar():
    if not frappe.db.get_single_value("Chatwoot Settings", "campanas_automaticas"):
        return

    hoy = frappe.utils.getdate(frappe.utils.today())

    for nombre in frappe.get_all("Campana Email", filters={"activa": 1}, pluck="name"):
        _avanzar_una(nombre, hoy)


def _avanzar_una(nombre, hoy):
    doc = frappe.get_doc("Campana Email", nombre)
    pasos = sorted(doc.pasos, key=lambda r: r.idx)

    fecha_base = frappe.utils.getdate(doc.creation)
    for paso in pasos:
        if paso.newsletter:
            nl_enviado, nl_fecha = frappe.db.get_value(
                "Newsletter", paso.newsletter, ["email_sent", "email_sent_at"]
            )
            if nl_enviado:
                fecha_base = frappe.utils.getdate(nl_fecha) if nl_fecha else fecha_base
                continue
            # Tiene contenido, no se ha enviado: ¿ya le toca?
            fecha_objetivo = (
                frappe.utils.getdate(paso.programado_para)
                if paso.programado_para
                else frappe.utils.add_days(fecha_base, frappe.utils.cint(paso.espera_dias or 0))
            )
            if hoy < fecha_objetivo:
                return  # todavía no le toca a este paso — nada más que hacer hoy
            _enviar(doc, paso)
            return  # un paso por corrida, igual que `secuencias.avanzar`
        else:
            # Sin contenido: la cadencia se detiene aquí, se avise o no.
            _avisar_paso_sin_contenido(doc, paso)
            return


def _enviar(campana_doc, paso):
    try:
        nl = frappe.get_doc("Newsletter", paso.newsletter)
        if nl.email_sent:
            return
        nl.send_emails()
        frappe.db.commit()
    except Exception:
        frappe.log_error(
            title=f"campana_email.avanzar — {campana_doc.name}",
            message=frappe.get_traceback(),
        )


def _avisar_paso_sin_contenido(campana_doc, paso):
    """Nota interna al equipo (misma superficie que otros avisos del CRM,
    `FCRM Note` en la propia campaña) — sin esto, una campaña programada que
    llega a un hueco de contenido se queda callada para siempre."""
    marcador = f"[campana-email-bloqueada:{paso.name}]"
    ya_avisado = frappe.db.exists(
        "FCRM Note",
        {
            "reference_doctype": "Campana Email",
            "reference_docname": campana_doc.name,
            "content": ["like", f"%{marcador}%"],
        },
    )
    if ya_avisado:
        return
    frappe.get_doc({
        "doctype": "FCRM Note",
        "title": "Campaña detenida — falta redactar un paso",
        "content": (
            f"La campaña \"{campana_doc.titulo}\" está programada pero el paso "
            f"\"{paso.titulo_paso}\" todavía no tiene contenido. No se manda "
            f"nada hasta que se redacte. {marcador}"
        ),
        "reference_doctype": "Campana Email",
        "reference_docname": campana_doc.name,
    }).insert(ignore_permissions=True)
    frappe.db.commit()
