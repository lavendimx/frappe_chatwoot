import frappe


def ejecutar():
    meta = frappe.get_meta("Agente IA")
    out = {}
    for fn in ["pausar_ante_intervencion_humana", "minutos_pausa_humana"]:
        f = meta.get_field(fn)
        out[fn] = {"fieldtype": f.fieldtype, "default": f.default} if f else None

    meta_rs = frappe.get_meta("Redes Sociales")
    out["redes_sociales_fields"] = [f.fieldname for f in meta_rs.fields]

    out["chatwoot_settings_switch"] = frappe.db.get_value(
        "Custom Field", "Chatwoot Settings-expirar_pausas_activo", ["fieldtype", "default"]
    )

    out["get_active_agentes_sample"] = None
    from frappe_chatwoot.frappe_chatwoot.api.agentes import get_active_agentes
    agentes = get_active_agentes()
    out["get_active_agentes_sample"] = agentes.get("5")

    return out
