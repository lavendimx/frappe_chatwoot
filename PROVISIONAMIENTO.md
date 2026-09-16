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
| Ajustes que los fixtures no pueden hacer (borrar etapas nativas) | `frappe_chatwoot/utils/provisionamiento.py` (`after_migrate`) | `bench migrate` |
| Datos del cliente (contactos, conversaciones, KB, plantillas, tokens) | DB + Chatwoot | **No viaja** — es por cliente |

## Alta de un sitio nuevo

```bash
bench new-site <cliente>.lavendi.mx --mariadb-root-password "$MARIADB_ROOT_PASSWORD" --admin-password <pass>
bench --site <cliente>.lavendi.mx install-app erpnext
bench --site <cliente>.lavendi.mx install-app crm
bench --site <cliente>.lavendi.mx install-app frappe_chatwoot
bench --site <cliente>.lavendi.mx migrate
bench build
```

Orden importa: `erpnext` primero (los fixtures tocan `Customer`, `Sales Invoice`,
`Payment Entry`). `frappe_chatwoot` al final para que sus fixtures corran con los
doctypes de `crm` ya presentes.

Queda **fuera** del provisionamiento, porque es decisión por cliente: inbox de
Chatwoot + número de WhatsApp, `Chatwoot Settings` (tokens de agenda y onboarding),
KB sources, plantillas, y encender el agente.

## Propagar una mejora ya hecha en crm.lavendi.mx

1. **Código** (`crm` o `frappe_chatwoot`): commit + push en la rama correspondiente.
   En cada sitio: `bench --site <sitio> update` (o `git pull` + `bench migrate` + `bench build`).
2. **Doctype nuevo o cambiado**: exportarlo a código
   (`bench --site crm.lavendi.mx execute frappe_chatwoot._exportar_doctypes.ejecutar`)
   y commit. Ojo: la clase del controlador se llama como el doctype sin espacios ni
   guiones (`Agente IA` → `AgenteIA`), o `get_controller` lanza `ImportError` y el
   doctype se borra como huérfano en el siguiente migrate.
3. **Campo / property setter / traducción / permiso / Web Form**: agregarlo a la DB de
   crm.lavendi.mx, luego `bench --site crm.lavendi.mx export-fixtures --app frappe_chatwoot`
   y commit. Los filtros viven en `hooks.py` (`fixtures = [...]`).
4. **Borrar algo** (etapa, campo): los fixtures no borran — va en
   `utils/provisionamiento.py` (como `ajustar_embudo`) o en un patch de la app.

## Trampas aprendidas

- **`bench new-app` en producción es peligroso**: registra la app en `sites/apps.txt`
  al instante; si la carpeta desaparece, bench no carga y el contenedor entra en
  crash-loop. Los doctypes van a una app ya instalada.
- **`get_controller`**: la clase debe llamarse igual que el doctype sin espacios ni
  guiones. Si no, `ImportError` → `remove_orphan_doctypes` lo borra.
- **`fixtures` no borra**: solo upsert. Los borrados necesitan hook o patch.
- **Cada paso de un `after_migrate` con su propio commit**: un rename que choca
  revierte los borrados de la misma corrida.
- **Los fixtures referencian doctypes de `erpnext`/`crm`**: un sitio sin esos apps
  salta el archivo entero ("Skipping fixture syncing... Reason: X not found").
