# Estado del Sistema: Integración de WhatsApp - Google AI Sales Coach Agent

Este documento detalla el estado actual del sistema, la arquitectura de integración con la API de WhatsApp Business de Meta y los pasos necesarios para completar la configuración con el **Tech Partner**.

---

## 1. Resumen Arquitectónico del Módulo WhatsApp

El sistema utiliza **FastAPI** como backend y tiene dos componentes clave para la integración con WhatsApp:

1. **Router de Webhook (`routers/whatsapp.py`)**:
   - **GET `/webhook/whatsapp`**: Endpoint de verificación requerido por Meta para validar la propiedad del webhook. Compara el token enviado por Meta con `WHATSAPP_VERIFY_TOKEN`.
   - **POST `/webhook/whatsapp`**: Endpoint receptor que procesa los eventos entrantes de WhatsApp:
     - Filtra mensajes tipo `text` y respuestas interactivas tipo `button_reply`.
     - Identifica al vendedor mediante su número de teléfono en la base de datos (PostgreSQL).
     - Si el usuario no está registrado, responde con un mensaje por defecto indicando que no está registrado.
     - Si el usuario está registrado, descifra su Token de Acceso de Google Workspace, recupera el historial de chat desde Redis y pasa el mensaje al **Agente Cognitivo Gemini** (`agent/gemini_agent.py`) o procesa la respuesta interactiva del flujo de accountability de reuniones (`AWAITING_MEETING_FEEDBACK`).

2. **Servicio de WhatsApp (`services/whatsapp_service.py`)**:
   - Clase helper `WhatsAppService` que se conecta a la API de Meta (`https://graph.facebook.com/v19.0/{phone_id}/messages`).
   - Métodos implementados:
     - `send_text_message`: Envío de mensajes de texto estándar con soporte para vista previa de enlaces.
     - `send_document_message`: Envío de documentos (como cotizaciones en formato PDF generadas dinámicamente) a través de una URL.
     - `send_interactive_buttons`: Envío de botones interactivos para flujos guiados (soporta hasta 3 botones por políticas de Meta, ej: "Sí, se realizó" / "No se realizó").
   - **Simulación (Mocking)**: Si las credenciales en la configuración son las de prueba por defecto (`EAAXxXX...` o `1234567890`), el servicio simula el envío registrando los payloads en la consola (`[MOCK WHATSAPP]`), lo cual permite probar la lógica de negocio localmente sin requerir una cuenta activa de WhatsApp Business.

---

## 2. Configuración de Entorno y Variables Requeridas

Las siguientes variables son leídas por `config/settings.py` y deben ser configuradas en el entorno local (usando un archivo `.env` en la raíz) o en el **GCP Secret Manager** en producción:

| Variable de Entorno | Descripción | Valor Actual de Desarrollo |
| :--- | :--- | :--- |
| `WHATSAPP_TOKEN` | Token de acceso de Meta (System User Access Token). | `"EAAXxXX..."` (Mock/Placeholder) |
| `WHATSAPP_PHONE_NUMBER_ID` | Identificador único del número de teléfono en Meta. | `"1234567890"` (Mock/Placeholder) |
| `WHATSAPP_VERIFY_TOKEN` | Token secreto definido por ti para verificar tu webhook en Meta. | `"my_secure_verify_token"` |

### Configuración en Secret Manager de GCP
El script de aprovisionamiento de infraestructura (`setup_gcp.sh`) ya creó los placeholders en GCP Secret Manager con los siguientes nombres:
- `COACHAGENT_WHATSAPP_TOKEN`
- `COACHAGENT_WHATSAPP_PHONE_NUMBER_ID`
- `COACHAGENT_WHATSAPP_VERIFY_TOKEN`

---

## 3. Estado de la Base de Datos y Dependencias

- **Base de Datos (PostgreSQL)**: 
  - La tabla `users` contiene una columna `phone_number` que asocia el número del vendedor (en formato internacional, ej. `52155...` o `54911...`) con su cuenta y tokens de Google Workspace.
  - La tabla `conversation_logs` almacena los registros históricos de los mensajes recibidos y enviados.
- **Base de Datos en Memoria (Redis)**:
  - Mantiene el historial de chat activo (`chat_history`) para el contexto conversacional del Gemini Agent.
  - Almacena los estados temporales de las llamadas interactivas, como el flujo de accountability de fin de día (`evening-accountability`).

---

## 4. Guía de Conexión y Próximos Pasos con el Tech Partner

Para conectar el sistema de producción con la cuenta real de WhatsApp a través del Tech Partner (o directamente con la consola de desarrolladores de Meta), sigue esta lista de verificación:

### Paso 1: Configurar Credenciales en Meta Developer Console
1. **Crear una App en Meta**: Asegúrate de tener una App de tipo "Business" en el portal de [Meta for Developers](https://developers.facebook.com/).
2. **Agregar el producto de WhatsApp**: Asocia la cuenta de WhatsApp Business al portal.
3. **Obtener el Phone Number ID**: Copia el ID único del número de teléfono del panel de control de WhatsApp.
4. **Obtener el System User Access Token**: 
   - Genera un token de acceso permanente (System User Token) en la configuración de Meta Business Suite con los permisos `whatsapp_business_messaging` y `whatsapp_business_management`.
   - *Nota: Evita usar el token temporal de 24 horas en producción.*

### Paso 2: Configurar y Registrar el Webhook
1. **Desplegar la Aplicación**: La aplicación debe estar corriendo en un entorno accesible públicamente con HTTPS (ej. Cloud Run en GCP).
2. **Definir el URL del Webhook**: En el portal de Meta Developers, configura el Webhook apuntando a:
   `https://[TU-DOMINIO-O-URL-CLOUD-RUN]/webhook/whatsapp`
3. **Definir el Token de Verificación**: Escribe un token secreto (por ejemplo, el valor de tu `WHATSAPP_VERIFY_TOKEN`).
4. **Verificar y Guardar**: Meta enviará un GET request a tu endpoint `/webhook/whatsapp` para validar. El sistema responderá con el reto (`hub.challenge`) si el token coincide.
5. **Suscribirse a Campos**: Suscríbete al campo de webhook **`messages`** dentro del producto de WhatsApp en el portal de Meta para recibir los mensajes entrantes de los usuarios.

### Paso 3: Actualizar Secrets en Google Cloud Platform
Actualiza las credenciales reales en Secret Manager en GCP o en tu archivo `.env` de producción:
- Establece `COACHAGENT_WHATSAPP_TOKEN` con el System User Access Token definitivo.
- Establece `COACHAGENT_WHATSAPP_PHONE_NUMBER_ID` con el ID del número telefónico productivo.
- Establece `COACHAGENT_WHATSAPP_VERIFY_TOKEN` con el token secreto utilizado en el webhook.
- Reinicia/Redespliega el contenedor de Cloud Run para cargar los nuevos valores.

---

## 5. Pruebas y Diagnóstico

- **Simulación Local**: Puedes chatear con el agente a través del playground web (`GET /`) y la API simulará la lógica de WhatsApp imprimiendo las respuestas en la consola.
- **Log de Webhook**: Para revisar qué payloads está recibiendo el backend desde Meta, busca la línea en el log que indica: `Incoming WhatsApp webhook payload: ...`
- **Filtro de Estado de Mensajes**: Meta envía eventos de estado como `sent`, `delivered` y `read`. Actualmente, el backend ignora estas notificaciones de manera segura para evitar ruido en el procesamiento de conversaciones.
