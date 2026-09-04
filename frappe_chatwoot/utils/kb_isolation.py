import frappe


def set_inbox_from_user_permission(doc, method):
	"""Fuerza inbox_id desde el User Permission del usuario logueado, ignorando
	cualquier valor que haya llegado en el documento (formulario web, API, etc).

	Defensa en profundidad: aunque el Web Form cliente-facing no exponga el
	campo inbox_id, este hook es la garantía real de aislamiento -- ni un
	cliente manipulando el request, ni un cambio futuro al Web Form que
	exponga el campo por error, puede hacer que un KB Source quede asociado
	a un inbox_id distinto al asignado al usuario.
	"""
	if frappe.session.user in ("Administrator", "Guest"):
		return  # Administrator: acceso total historico

	# Personal interno de lavendi.mx (System Manager) y el usuario de servicio del
	# worker de ingesta: pueden crear KB Sources para cualquier inbox_id, igual que
	# Administrator. Necesario desde que se roto la API key de Administrator hacia
	# el usuario dedicado agente-ia@lavendi.mx -- sin esto nadie de adentro podia
	# dar de alta conocimiento. Los clientes del portal (rol "Cliente KB", Website
	# User, sin System Manager) SI siguen forzados por el bloque de abajo.
	if "System Manager" in frappe.get_roles():
		return

	allowed = frappe.get_all(
		"User Permission",
		filters={"user": frappe.session.user, "allow": "KB Inbox"},
		pluck="for_value",
		limit=1,
	)
	if not allowed:
		frappe.throw("Tu usuario no tiene un cliente KB asignado. Contacta a soporte de lavendi.mx.")

	doc.inbox_id = allowed[0]
