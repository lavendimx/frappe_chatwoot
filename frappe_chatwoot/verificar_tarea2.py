import frappe


def ejecutar():
    out = {}

    # 1. Job de expiración con el switch apagado -> debe ser no-op
    from frappe_chatwoot.utils.pausas import expirar_pausas
    out["expirar_pausas_switch_off"] = expirar_pausas()

    # 2. estado() de Redes Sociales sin configurar nada -> no debe llamar a Ayrshare
    frappe.set_user("Administrator")
    from frappe_chatwoot.frappe_chatwoot.api.redes_sociales import estado
    out["estado_sin_configurar"] = estado()

    # 3. generar_link_conexion() sin 'enabled' -> debe frappe.throw con mensaje claro
    from frappe_chatwoot.frappe_chatwoot.api.redes_sociales import generar_link_conexion
    try:
        generar_link_conexion()
        out["generar_link_sin_activar"] = "NO LANZO (mal)"
    except frappe.ValidationError as e:
        out["generar_link_sin_activar"] = f"OK, lanzo: {e}"

    return out
