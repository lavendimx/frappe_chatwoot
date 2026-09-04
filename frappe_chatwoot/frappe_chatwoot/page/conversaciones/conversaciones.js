frappe.pages['conversaciones'].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: 'Conversaciones',
		single_column: true,
	});

	new Conversaciones(page);
};

class Conversaciones {
	constructor(page) {
		this.page = page;
		this.current_conversation_id = null;
		this.poll_timer = null;
		this.make_toolbar();
		this.make_body();
	}

	make_toolbar() {
		this.reference_doctype_field = this.page.add_field({
			label: 'Tipo',
			fieldname: 'reference_doctype',
			fieldtype: 'Select',
			options: ['Contact', 'CRM Lead', 'CRM Deal'],
			default: 'Contact',
			change: () => this.on_doctype_change(),
		});

		// Un campo "Link" normal en vez de "Dynamic Link": este último solo
		// resuelve el doctype destino cuando el control vive dentro de un
		// formulario ligado a un documento real (lee el campo hermano vía
		// frappe.model.get_value) — en un page.add_field ad-hoc no hay
		// documento detrás, así que nunca dispara la búsqueda. Con "Link"
		// controlamos el doctype destino a mano (df.options) cada vez que
		// cambia el selector de arriba.
		this.reference_name_field = this.page.add_field({
			label: 'Contacto',
			fieldname: 'reference_name',
			fieldtype: 'Link',
			options: 'Contact',
			change: () => this.on_reference_change(),
		});
	}

	on_doctype_change() {
		let doctype = this.reference_doctype_field.get_value();
		this.reference_name_field.df.options = doctype;
		this.reference_name_field.set_value('');
		this.reference_name_field.refresh();
	}

	on_reference_change() {
		let doctype = this.reference_doctype_field.get_value();
		let name = this.reference_name_field.get_value();
		if (!doctype || !name) return;
		this.load_conversations(doctype, name);
	}

	make_body() {
		this.$body = $(`
			<div class="conversaciones-wrapper" style="display:flex; gap:16px; margin-top:12px;">
				<div class="conversaciones-list" style="width:280px; flex-shrink:0; border-right:1px solid var(--border-color); padding-right:12px;">
					<div class="text-muted" style="padding:8px 0;">Elige un contacto arriba para ver sus conversaciones.</div>
				</div>
				<div class="conversaciones-thread" style="flex:1; display:flex; flex-direction:column; min-height:400px;">
					<div class="thread-messages" style="flex:1; overflow-y:auto; max-height:60vh; padding:8px;"></div>
					<div class="thread-reply" style="display:none; border-top:1px solid var(--border-color); padding-top:8px; margin-top:8px;">
						<textarea class="form-control reply-text" rows="2" placeholder="Escribe una respuesta..."></textarea>
						<button class="btn btn-primary btn-sm reply-send" style="margin-top:6px;">Enviar</button>
					</div>
				</div>
			</div>
		`).appendTo(this.page.main);

		this.$list = this.$body.find('.conversaciones-list');
		this.$messages = this.$body.find('.thread-messages');
		this.$reply_box = this.$body.find('.thread-reply');
		this.$reply_text = this.$body.find('.reply-text');

		this.$body.find('.reply-send').on('click', () => this.send_reply());
	}

	load_conversations(reference_doctype, reference_name) {
		this.$list.html('<div class="text-muted" style="padding:8px 0;">Buscando conversaciones...</div>');
		frappe.call({
			method: 'frappe_chatwoot.frappe_chatwoot.api.chatwoot.get_conversations_for_contact',
			args: { reference_doctype, reference_name },
			callback: (r) => {
				let conversations = r.message || [];
				this.render_conversation_list(conversations);
			},
			error: () => {
				this.$list.html('<div class="text-danger" style="padding:8px 0;">No se pudieron cargar las conversaciones.</div>');
			},
		});
	}

	render_conversation_list(conversations) {
		if (!conversations.length) {
			this.$list.html('<div class="text-muted" style="padding:8px 0;">Sin conversaciones para este contacto.</div>');
			return;
		}
		this.$list.empty();
		conversations.forEach((conv) => {
			let $item = $(`
				<div class="conversation-item" data-id="${conv.id}" style="padding:8px; border-radius:6px; cursor:pointer; margin-bottom:4px;">
					<div style="font-weight:600;">${frappe.utils.escape_html(conv.meta?.sender?.name || 'Conversación #' + conv.id)}</div>
					<div class="text-muted" style="font-size:12px;">Estado: ${frappe.utils.escape_html(conv.status || '-')}</div>
				</div>
			`).appendTo(this.$list);

			$item.on('click', () => {
				this.$list.find('.conversation-item').css('background', '');
				$item.css('background', 'var(--bg-light-gray)');
				this.open_conversation(conv.id);
			});
		});
	}

	open_conversation(conversation_id) {
		this.current_conversation_id = conversation_id;
		this.$reply_box.show();
		this.load_messages();
		if (this.poll_timer) clearInterval(this.poll_timer);
		this.poll_timer = setInterval(() => this.load_messages(true), 8000);
	}

	load_messages(silent) {
		if (!this.current_conversation_id) return;
		frappe.call({
			method: 'frappe_chatwoot.frappe_chatwoot.api.chatwoot.get_messages',
			args: { conversation_id: this.current_conversation_id },
			callback: (r) => {
				let data = r.message || {};
				this.render_messages(data.messages || []);
			},
			error: () => {
				if (!silent) frappe.msgprint(__('No se pudo cargar la conversación.'));
			},
		});
	}

	render_messages(messages) {
		this.$messages.empty();
		messages.forEach((msg) => {
			let is_outgoing = msg.direction === 'outgoing';
			let $bubble = $(`
				<div style="display:flex; justify-content:${is_outgoing ? 'flex-end' : 'flex-start'}; margin-bottom:8px;">
					<div style="max-width:70%; padding:8px 12px; border-radius:10px; background:${is_outgoing ? 'var(--blue-100)' : 'var(--bg-light-gray)'};">
						<div>${frappe.utils.escape_html(msg.content || '')}</div>
						<div class="text-muted" style="font-size:11px; margin-top:2px;">${moment.unix(msg.created_at).format('DD-MMM HH:mm')}</div>
					</div>
				</div>
			`);
			this.$messages.append($bubble);
		});
		this.$messages.scrollTop(this.$messages[0].scrollHeight);
	}

	send_reply() {
		let content = this.$reply_text.val().trim();
		if (!content || !this.current_conversation_id) return;
		frappe.call({
			method: 'frappe_chatwoot.frappe_chatwoot.api.chatwoot.send_message',
			args: { conversation_id: this.current_conversation_id, content },
			callback: () => {
				this.$reply_text.val('');
				this.load_messages();
			},
			error: () => {
				frappe.msgprint(__('No se pudo enviar el mensaje.'));
			},
		});
	}
}
