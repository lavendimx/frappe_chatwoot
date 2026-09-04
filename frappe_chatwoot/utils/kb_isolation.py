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
		return  # Administrator = worker de ingesta y API interna; ya manda el inbox_id correcto

	allowed = frappe.get_all(
		"User Permission",
		filters={"user": frappe.session.user, "allow": "KB Inbox"},
		pluck="for_value",
		limit=1,
	)
	if not allowed:
		frappe.throw("Tu usuario no tiene un cliente KB asignado. Contacta a soporte de lavendi.mx.")

	doc.inbox_id = allowed[0]
