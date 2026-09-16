# Provisionamiento de un sitio de cliente (Sofía CRM)

Cómo se propaga una mejora de `crm.lavendi.mx` a un sitio nuevo (Six Gardens y los
que vengan). Antes de esto la propagación era a mano, sitio por sitio, y la rama
`lavendi-sofia` de `crm` no estaba en ningún remoto: un rebuild del volumen
`frappe-home` se la llevaba.

## De qué consta un sitio

| Capa | Dónde vive | Cómo viaja |
|---|---|---|
| App `crm` (SPA + backend) | `lavendimx/crm`, rama **`lavendi-sofia`** (privado) | `bench get-app crm <url> --branch lavendi-sofia` |
| App `frappe_chatwoot` (API, utils, agentes) | `lavendimx/frappe_chatwoot`, rama `master` | `bench get-app frappe_chatwoot <url>` |
| 15 doctypes propios | `frappe_chatwoot/frappe_chatwoot/doctype/*` (código) | `bench migrate` los crea |
| Campos, property setters, traducciones, permisos, Web Forms, embudo/orígenes/razones | `frappe_chatwoot/frappe_chatwoot/fixtures/*.json` | `bench migrate` los importa |
| Lo que depende de `erpnext` (`Customer`, `Sales Invoice`, `Payment Entry`) | `frappe_chatwoot/frappe_chatwoot/fixtures/erpnext/*.json` | `after_migrate`, **solo si erpnext está instalado** |
| Ajustes que los fixtures no pueden hacer (borrar etapas nativas) | `frappe_chatwoot/utils/provisionamiento.py` (`after_migrate`) | `bench migrate` |
| Datos del cliente (contactos, conversaciones, KB, plantillas, tokens) | DB + Chatwoot | **No viaja** — es por cliente |

**No hay un branch por sitio.** El código es uno solo, compartido por todos los sitios
del bench. Lo que cambia por sitio es (a) qué apps están instaladas y (b) la config de
su DB. Un sitio "básico" (frappe + crm + frappe_chatwoot, sin erpnext) recibe todo
menos lo de la fila de erpnext.

## Alta de un sitio nuevo

```bash
bench new-site <cliente>.lavendi.mx --mariadb-root-password "$MARIADB_ROOT_PASSWORD" --admin-password <pass>
bench --site <cliente>.lavendi.mx install-app crm
bench --site <cliente>.lavendi.mx install-app frappe_chatwoot
# solo si el cliente lleva facturación:
bench --site <cliente>.lavendi.mx install-app erpnext
bench --site <cliente>.lavendi.mx migrate
bench build
```

`frappe_chatwoot` al final: sus fixtures tocan doctypes de `crm`. `erpnext` en cualquier
orden (sus dependencias van en `fixtures/erpnext/`, que se importan solas si está).

Queda **fuera** del provisionamiento, porque es decisión por cliente: inbox de Chatwoot
+ número de WhatsApp, `Chatwoot Settings` (tokens de agenda y onboarding), KB sources,
plantillas, y encender el agente.

## Propagar una mejora ya hecha en crm.lavendi.mx

1. **Código** (`crm` o `frappe_chatwoot`): commit + push en la rama correspondiente.
   En cada sitio: `bench --site <sitio> update` (o `git pull` + `bench migrate` + `bench build`).
2. **Doctype nuevo o cambiado**: exportarlo a código
   (`bench --site crm.lavendi.mx execute frappe_chatwoot._exportar_doctypes.ejecutar`)
   y commit.
3. **Campo / property setter / traducción / permiso / Web Form**: agregarlo a la DB de
   crm.lavendi.mx, luego `bench --site crm.lavendi.mx export-fixtures --app frappe_chatwoot`
   y commit. Los filtros viven en `hooks.py` (`fixtures = [...]`).
   Si el doctype destino es de `erpnext` (o de cualquier app opcional), va a
   `fixtures/erpnext/` — no a `fixtures/`.
4. **Borrar algo** (etapa, campo): los fixtures no borran — va en
   `utils/provisionamiento.py` (como `_borrar_etapas_nativas`) o en un patch de la app.

## Trampas aprendidas

- **`bench new-app` en producción es peligroso**: registra la app en `sites/apps.txt`
  al instante; si la carpeta desaparece, bench no carga y el contenedor entra en
  crash-loop. Los doctypes van a una app ya instalada.
- **Los fixtures saltan el ARCHIVO COMPLETO, no el registro**: `frappe/utils/fixtures.py`
  envuelve cada `.json` en un `try/except`; si un solo doctype del archivo no existe,
  no se importa nada de ese archivo. Por eso lo que depende de una app opcional va en
  `fixtures/erpnext/`.
- **Las child tables no se exportan como fixture**: al importarlas Frappe las vuelve a
  insertar y duplica los campos (pasó con `Web Form Field`: 24 → 48). Los campos de un
  Web Form viajan dentro de `web_form.json`.
- **`get_controller`**: la clase del controlador debe llamarse igual que el doctype sin
  espacios ni guiones (`Agente IA` → `AgenteIA`). Si no, `ImportError` →
  `remove_orphan_doctypes` **borra** el doctype en el siguiente migrate.
- **Los doctypes van en la carpeta del módulo**, no en la raíz del paquete:
  `apps/frappe_chatwoot/frappe_chatwoot/frappe_chatwoot/doctype/` (la del `modules.txt`).
- **Cada paso de un `after_migrate` con su propio commit**: un rename que choca revierte
  los borrados de la misma corrida.
