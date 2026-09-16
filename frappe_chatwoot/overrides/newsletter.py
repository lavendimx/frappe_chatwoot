# Copyright (c) 2026, lavendi.mx
#
# Newsletter con personalización POR DESTINATARIO.
#
# Por qué existe este override (verificado en el core el 2026-09-16):
# `Newsletter.send_newsletter()` arma UN solo `args` para toda la lista —
# `get_message()` renderiza una vez con `{"doc": self.as_dict()}` y el template
# `templates/emails/newsletter.html` solo inyecta `{{ message }}`. Resultado:
# `{{ contact.first_name }}` salía LITERAL para todos los destinatarios.
#
# Este override reemplaza únicamente `send_newsletter()` y `get_message()`:
# recorre los destinatarios uno por uno, resuelve el `Contact` por correo y
# renderiza el HTML con los merge fields de ESE contacto. Todo lo demás
# (tracking de apertura y clic, link de baja, adjuntos, `queue_all`,
# `send_test_email`, `send_emails`, validaciones) se hereda sin cambios.
#
# Se registra con `override_doctype_class` en hooks.py — NO se edita el core,
# así sobrevive a un `bench update`. Es parte del sistema replicable a clientes
# que sustituye a GHL, no un parche de una sola campaña.
#
# Convención de merge fields: la MISMA del motor de secuencias
# (`utils/secuencias.py`), para que una plantilla sirva en ambos motores:
#     {{ contact.first_name }} · {{ contact.last_name }}
#     {{ contact.company_name }} · {{ contact.email }}
#
# Si el contacto no tiene nombre, `{{ contact.first_name }}` sale vacío — la
# plantilla es responsable de caer a un saludo genérico:
#     {% if contact.first_name %}Hola {{ contact.first_name }},{% else %}Hola,{% endif %}

import frappe
import frappe.utils
from frappe.email.doctype.newsletter.newsletter import Newsletter as _Newsletter

_TRACKER = "/api/method/frappe.email.doctype.newsletter.newsletter.newsletter_email_read"

_CAMPOS = ["name", "first_name", "last_name", "company_name", "email_id"]


def contacto_por_correo(email):
	"""Resuelve el Contact dueño de ese correo, listo para el contexto Jinja.

	Busca primero en el campo principal `email_id` y luego en la tabla hija
	`Contact Email` (un contacto puede tener varios correos).

	SIEMPRE devuelve el dict con las 5 llaves. Nunca `{}`: con el `Undefined` de
	Jinja de Frappe, una llave ausente renderiza literal
	`{{ no such element: dict object['first_name'] }}` en el correo — feo y
	visible al destinatario. Con la llave presente y vacía, sale `""`.
	"""
	email = (email or "").strip()
	vacio = {"name": "", "first_name": "", "last_name": "", "company_name": "", "email": email}

	if not email:
		return vacio

	contacto = frappe.db.get_value("Contact", {"email_id": email}, _CAMPOS, as_dict=True)

	if not contacto:
		parent = frappe.db.get_value("Contact Email", {"email_id": email}, "parent")
		if parent:
			contacto = frappe.db.get_value("Contact", parent, _CAMPOS, as_dict=True)

	if not contacto:
		return vacio

	return {
		"name": contacto.name,
		"first_name": (contacto.first_name or "").strip(),
		"last_name": (contacto.last_name or "").strip(),
		"company_name": (contacto.company_name or "").strip(),
		"email": (contacto.email_id or email).strip(),
	}


class Newsletter(_Newsletter):
	def send_newsletter(self, emails, test_email=False):
		"""Encola un correo POR destinatario, con sus merge fields resueltos."""
		attachments = self.get_newsletter_attachments()
		sender = self.send_from or frappe.utils.get_formatted_email(self.owner)

		is_auto_commit_set = bool(frappe.db.auto_commit_on_many_writes)
		frappe.db.auto_commit_on_many_writes = not frappe.flags.in_test

		try:
			for email in emails:
				args = self.as_dict()
				args["message"] = self.get_message(medium="email", contacto=email)

				frappe.sendmail(
					subject=self.subject,
					sender=sender,
					recipients=[email],
					attachments=attachments,
					template="newsletter",
					add_unsubscribe_link=self.send_unsubscribe_link,
					unsubscribe_method="/unsubscribe",
					unsubscribe_params={"name": self.name},
					reference_doctype=self.doctype,
					reference_name=self.name,
					send_priority=0,
					args=args,
					email_read_tracker_url=None if test_email else _TRACKER,
				)
		finally:
			frappe.db.auto_commit_on_many_writes = is_auto_commit_set

	def get_message(self, medium=None, contacto=None):
		"""Renderiza el HTML con el contexto del contacto (además del `doc`)."""
		message = self.message
		if self.content_type == "Markdown":
			message = frappe.utils.md_to_html(self.message_md)
		if self.content_type == "HTML":
			message = self.message_html

		contexto = {"doc": self.as_dict(), "contact": contacto_por_correo(contacto)}
		html = frappe.render_template(message, contexto)

		return self.add_source(html, medium=medium)
