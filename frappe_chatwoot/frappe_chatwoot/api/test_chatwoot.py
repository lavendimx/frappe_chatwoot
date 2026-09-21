# Copyright (c) 2026, Hypedrive
# License: MIT

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_chatwoot.frappe_chatwoot.api import chatwoot as api
from frappe_chatwoot.utils import chatwoot_client as cw


def _configure_settings(enabled=1):
    settings = frappe.get_single("Chatwoot Settings")
    settings.enabled = enabled
    settings.base_url = "https://support.hypedrive.app"
    settings.account_id = 1
    settings.api_token = "dummy-token-for-test"
    settings.save()
    return settings


class TestChatwootApiGracefulDegradation(FrappeTestCase):
    """When Chatwoot Settings is missing/disabled, every read-path API
    should degrade softly (return empty/False) rather than raising — this
    is the documented contract shared with crm.api.whatsapp's own
    graceful-degradation behavior."""

    def tearDown(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()

    def test_is_chatwoot_enabled_false_when_disabled(self):
        _configure_settings(enabled=0)
        self.assertFalse(api.is_chatwoot_enabled())

    def test_is_chatwoot_enabled_true_when_configured(self):
        _configure_settings(enabled=1)
        self.assertTrue(api.is_chatwoot_enabled())

    def test_is_chatwoot_installed_true(self):
        # frappe_chatwoot is installed in this test site by definition.
        self.assertTrue(api.is_chatwoot_installed())

    def test_get_conversations_for_contact_degrades_to_empty_list_when_disabled(self):
        _configure_settings(enabled=0)
        result = api.get_conversations_for_contact("CRM Lead", "does-not-matter")
        self.assertEqual(result, [])

    def test_get_messages_throws_when_disabled(self):
        """Unlike the conversation-list lookup (soft-degrade to []),
        get_messages requires an already-resolved conversation_id and
        throws rather than silently returning an empty page — mirrors the
        documented contract in api/chatwoot.py's docstring."""
        _configure_settings(enabled=0)
        with self.assertRaises(frappe.ValidationError):
            api.get_messages(1)

    def test_send_message_throws_when_disabled(self):
        _configure_settings(enabled=0)
        with self.assertRaises(frappe.ValidationError):
            api.send_message(1, "hello")


class TestChatwootApiRoleGate(FrappeTestCase):
    """validate_access enforces ALLOWED_ROLES for non-Administrator users."""

    def setUp(self):
        _configure_settings(enabled=1)
        self.test_user = "test_chatwoot_role_gate@example.com"
        if not frappe.db.exists("User", self.test_user):
            frappe.get_doc({
                "doctype": "User",
                "email": self.test_user,
                "first_name": "Chatwoot",
                "last_name": "RoleGateTest",
                "send_welcome_email": 0,
                "roles": [{"role": "Sales Manager"}],
            }).insert(ignore_permissions=True)

        self.lead = frappe.get_doc({
            "doctype": "CRM Lead" if frappe.db.exists("DocType", "CRM Lead") else "Contact",
        })

    def tearDown(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()

    def test_missing_reference_raises(self):
        with self.assertRaises(frappe.ValidationError):
            api.validate_access("", "")

    def test_role_outside_allowlist_raises_permission_error(self):
        if not frappe.db.exists("DocType", "CRM Lead"):
            self.skipTest("CRM Lead doctype not installed in this test env")

        no_access_user = "test_chatwoot_no_access@example.com"
        if not frappe.db.exists("User", no_access_user):
            frappe.get_doc({
                "doctype": "User",
                "email": no_access_user,
                "first_name": "NoAccess",
                "last_name": "Test",
                "send_welcome_email": 0,
                "roles": [{"role": "Guest"}] if frappe.db.exists("Role", "Guest") else [],
            }).insert(ignore_permissions=True)

        lead = frappe.get_doc({
            "doctype": "CRM Lead",
            "lead_name": "Chatwoot Role Gate Test Lead",
        }).insert(ignore_permissions=True)

        with patch.object(frappe, "session") as mock_session:
            mock_session.user = no_access_user
            with self.assertRaises(frappe.PermissionError):
                api.validate_access("CRM Lead", lead.name)

        frappe.delete_doc("CRM Lead", lead.name, force=True, ignore_permissions=True)

    def test_administrator_bypasses_role_allowlist(self):
        if not frappe.db.exists("DocType", "CRM Lead"):
            self.skipTest("CRM Lead doctype not installed in this test env")

        lead = frappe.get_doc({
            "doctype": "CRM Lead",
            "lead_name": "Chatwoot Admin Bypass Test Lead",
        }).insert(ignore_permissions=True)

        # Administrator is exempted from the ALLOWED_ROLES gate explicitly
        # in validate_access's condition.
        doc = api.validate_access("CRM Lead", lead.name)
        self.assertEqual(doc.name, lead.name)

        frappe.delete_doc("CRM Lead", lead.name, force=True, ignore_permissions=True)


class TestChatwootApiMessageDirectionMapping(FrappeTestCase):
    def test_annotate_direction_maps_all_known_codes(self):
        messages = [
            {"id": 1, "message_type": 0},
            {"id": 2, "message_type": 1},
            {"id": 3, "message_type": 2},
            {"id": 4, "message_type": 3},
            {"id": 5, "message_type": 99},
        ]
        result = api._annotate_direction(messages)
        directions = {m["id"]: m["direction"] for m in result}
        self.assertEqual(directions, {
            1: "incoming",
            2: "outgoing",
            3: "activity",
            4: "template",
            5: "unknown",
        })


class TestChatwootApiSendMessageValidation(FrappeTestCase):
    def setUp(self):
        _configure_settings(enabled=1)

    def tearDown(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()

    def test_empty_content_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            api.send_message(1, "")

    def test_whitespace_only_content_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            api.send_message(1, "   ")

    @patch("frappe_chatwoot.utils.chatwoot_client.create_message")
    def test_content_is_stripped_before_send(self, mock_create):
        mock_create.return_value = {"id": 1}
        api.send_message(1, "  hello world  ")
        mock_create.assert_called_once_with(1, "hello world")


class TestConversationEndpointsRoleGate(FrappeTestCase):
    """The conversation_id-keyed endpoints have no reference-doc context to
    check, but must still enforce the role allowlist so they are not an
    unguarded /api/method/ path around crm.api.chatwoot's ownership check."""

    def setUp(self):
        _configure_settings(enabled=1)

    def tearDown(self):
        settings = frappe.get_single("Chatwoot Settings")
        settings.enabled = 0
        settings.save()

    def test_get_messages_rejects_user_without_sales_role(self):
        with patch("frappe.get_roles", return_value=["Guest"]):
            with patch.object(frappe, "session") as mock_session:
                mock_session.user = "guest@example.com"
                with self.assertRaises(frappe.PermissionError):
                    api.get_messages(1)

    def test_get_new_messages_rejects_user_without_sales_role(self):
        with patch("frappe.get_roles", return_value=["Guest"]):
            with patch.object(frappe, "session") as mock_session:
                mock_session.user = "guest@example.com"
                with self.assertRaises(frappe.PermissionError):
                    api.get_new_messages(1)

    def test_send_message_rejects_user_without_sales_role(self):
        with patch("frappe.get_roles", return_value=["Guest"]):
            with patch.object(frappe, "session") as mock_session:
                mock_session.user = "guest@example.com"
                with self.assertRaises(frappe.PermissionError):
                    api.send_message(1, "hello")

    @patch("frappe_chatwoot.utils.chatwoot_client.create_message")
    def test_send_message_allowed_for_sales_user(self, mock_create):
        mock_create.return_value = {"id": 1}
        with patch("frappe.get_roles", return_value=["Sales User"]):
            with patch.object(frappe, "session") as mock_session:
                mock_session.user = "sales@example.com"
                api.send_message(1, "hello")
        mock_create.assert_called_once_with(1, "hello")


class TestChatwootApiClearCache(FrappeTestCase):
    def test_clear_cache_requires_system_manager(self):
        with patch("frappe.get_roles", return_value=["Sales User"]):
            with self.assertRaises(frappe.PermissionError):
                api.clear_chatwoot_cache()

    @patch("frappe_chatwoot.utils.chatwoot_client.clear_cache")
    def test_clear_cache_succeeds_for_system_manager(self, mock_clear):
        with patch("frappe.get_roles", return_value=["System Manager"]):
            result = api.clear_chatwoot_cache()
        self.assertEqual(result, {"ok": True})
        mock_clear.assert_called_once()


def _conv(cid, identifier=None, nombre="Alguien"):
    """Conversación con la forma que devuelve la API de lista de Chatwoot —
    lo mínimo que _shape_conversation lee."""
    return {
        "id": cid,
        "inbox_id": 5,
        "status": "open",
        "meta": {"sender": {"name": nombre, "identifier": identifier}},
        "last_non_activity_message": {"content": "hola", "message_type": 0},
    }


class TestDeteccionDeGrupo(FrappeTestCase):
    """Los grupos de WhatsApp se separan de las conversaciones con personas por
    el JID que Evolution guarda en el identifier del contacto — nunca por el
    nombre, que cualquiera puede cambiar desde Chatwoot."""

    def test_jid_de_grupo_es_grupo(self):
        self.assertTrue(api._es_grupo({"identifier": "120363041982978889@g.us"}))

    def test_jid_individual_no_es_grupo(self):
        self.assertFalse(api._es_grupo({"identifier": "5213330067027@s.whatsapp.net"}))

    def test_contacto_sin_identifier_no_es_grupo(self):
        # Contacto creado a mano desde la UI: solo tiene teléfono.
        self.assertFalse(api._es_grupo({"identifier": None}))
        self.assertFalse(api._es_grupo({}))

    def test_el_nombre_no_decide(self):
        # 9 de 9 grupos reales traen "(GROUP)" en el nombre, pero es renombrable.
        self.assertFalse(api._es_grupo({"name": "Equipo (GROUP)", "identifier": "521@s.whatsapp.net"}))
        self.assertTrue(api._es_grupo({"name": "Sin marca", "identifier": "1203@g.us"}))

    def test_shape_expone_is_group(self):
        grupo = api._shape_conversation(_conv(1, "1203@g.us"), set(), set())
        persona = api._shape_conversation(_conv(2, "521@s.whatsapp.net"), set(), set())
        self.assertTrue(grupo["is_group"])
        self.assertFalse(persona["is_group"])


class TestBarridoDeLaBandeja(FrappeTestCase):
    """get_conversations barre TODAS las páginas de Chatwoot. Antes devolvía la
    página 1 y dejaba fuera la mayor parte del canal (medido: 25 de 91, y 1 de 9
    grupos) — con la lista partida en secciones eso vaciaba la de grupos."""

    def setUp(self):
        frappe.cache().delete_value("frappe_chatwoot:bandeja_profunda:5:all")

    def tearDown(self):
        frappe.cache().delete_value("frappe_chatwoot:bandeja_profunda:5:all")

    def _paginas(self, *paginas):
        def fake(inbox_id=None, status=None, page=1):
            return paginas[page - 1] if page <= len(paginas) else []
        return fake

    def test_junta_todas_las_paginas(self):
        with patch.object(api.cw, "list_conversations", side_effect=self._paginas(
            [_conv(1)], [_conv(2)], [_conv(3)]
        )):
            ids = [c["id"] for c in api._barrer_canal(5, "all")]
        self.assertEqual(sorted(ids), [1, 2, 3])

    def test_pagina_uno_vacia_corta_sin_pedir_mas(self):
        with patch.object(api.cw, "list_conversations", side_effect=self._paginas([])) as m:
            self.assertEqual(api._barrer_canal(5, "all"), [])
        self.assertEqual(m.call_count, 1)

    def test_la_version_fresca_gana_sobre_la_cacheada(self):
        # Una conversación que sube a la página 1 sigue en la copia cacheada de
        # su página vieja; la buena es la nueva.
        vieja = _conv(7, nombre="Viejo")
        nueva = _conv(7, nombre="Nuevo")
        with patch.object(api.cw, "list_conversations", side_effect=self._paginas([nueva], [vieja])):
            res = api._barrer_canal(5, "all")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["meta"]["sender"]["name"], "Nuevo")

    def test_las_paginas_profundas_quedan_en_cache(self):
        # 2ª llamada: solo la página 1 vuelve a pedirse (1 petición, no 3).
        with patch.object(api.cw, "list_conversations", side_effect=self._paginas(
            [_conv(1)], [_conv(2)], [_conv(3)]
        )) as m:
            api._barrer_canal(5, "all")
            llamadas_frio = m.call_count
        with patch.object(api.cw, "list_conversations", side_effect=self._paginas(
            [_conv(1)], [_conv(2)], [_conv(3)]
        )) as m2:
            ids = [c["id"] for c in api._barrer_canal(5, "all")]
        self.assertEqual(llamadas_frio, 4)  # 3 con datos + 1 vacía que corta
        self.assertEqual(m2.call_count, 1)
        self.assertEqual(sorted(ids), [1, 2, 3])

    def test_tope_de_paginas_se_registra_en_el_log(self):
        infinita = lambda inbox_id=None, status=None, page=1: [_conv(page)]
        with patch.object(api.cw, "list_conversations", side_effect=infinita):
            with patch.object(api, "MAX_PAGINAS_BANDEJA", 3):
                with patch("frappe.log_error") as log:
                    res = api._barrer_canal(5, "all")
        log.assert_called_once()
        self.assertEqual(len(res), 3)


class TestArchivadoEnLaBandeja(FrappeTestCase):
    """Archivar es una bandera del CRM, no el estado de Chatwoot: la bandeja
    muestra o las vivas o las archivadas, nunca mezcladas."""

    def test_shape_expone_archived(self):
        marcada = api._shape_conversation(_conv(9), set(), {9})
        libre = api._shape_conversation(_conv(10), set(), set())
        self.assertTrue(marcada["archived"])
        self.assertFalse(libre["archived"])

    def test_shape_expone_agent_paused_sin_consultar_la_db(self):
        pausada = api._shape_conversation(_conv(11), {11}, set())
        self.assertTrue(pausada["agent_paused"])

    def test_ids_marcados_degrada_si_el_doctype_no_existe(self):
        # Instalación a medio migrar: la bandeja no debe tumbarse.
        self.assertEqual(api._ids_marcados("Doctype Que No Existe"), set())


class TestResolverConversacionPorId(FrappeTestCase):
    """Los enlaces directos (?conv=, ruta móvil, buscador global, push) no pueden
    depender del filtro de la lista: una conversación archivada o de otro canal
    no aparecía ahí y la pantalla quedaba vacía sin explicar por qué."""

    def test_devuelve_la_conversacion_con_forma_de_bandeja(self):
        _configure_settings(enabled=1)
        try:
            with patch.object(api.cw, "get_conversation", return_value=_conv(5, "1203@g.us")):
                r = api.obtener_conversacion(5)
        finally:
            _configure_settings(enabled=0)
        self.assertEqual(r["id"], 5)
        self.assertTrue(r["is_group"])
        self.assertIn("archived", r)

    def test_id_invalido_devuelve_none(self):
        _configure_settings(enabled=1)
        try:
            self.assertIsNone(api.obtener_conversacion(0))
        finally:
            _configure_settings(enabled=0)

    def test_degrada_a_none_si_chatwoot_no_responde(self):
        _configure_settings(enabled=1)
        try:
            with patch.object(api.cw, "get_conversation", side_effect=cw.ChatwootAPIError("caido")):
                self.assertIsNone(api.obtener_conversacion(5))
        finally:
            _configure_settings(enabled=0)

    def test_degrada_a_none_si_chatwoot_esta_deshabilitado(self):
        _configure_settings(enabled=0)
        self.assertIsNone(api.obtener_conversacion(5))
