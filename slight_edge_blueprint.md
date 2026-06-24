# Especificación de Arquitectura: Módulo "La Ligera Ventaja" (Slight Edge Engine)

Este documento sirve como el **Blueprint de Desarrollo** y base de conocimiento técnico para duplicar, migrar o reconstruir el sistema de coaching de ventas de **La Ligera Ventaja** (basado en la metodología de Jeff Olson y el Efecto Compuesto de Darren Hardy) en cualquier otro proyecto o stack tecnológico.

---

## 1. Concepto y Flujo de Negocio

El sistema está diseñado bajo el modelo **AAS (Agent as a Service)** y opera en base a la consistencia de pequeñas acciones diarias acumuladas. El flujo se compone de cuatro fases:

1. **Sesión Inicial de Coaching (Interactiva)**: El vendedor chatea con un Coach de IA. El Coach pregunta por su meta de ingresos mensuales, su ticket de venta promedio y su tasa de conversión. Basándose en el embudo de ventas, propone disciplinas diarias con un valor en puntos proporcional a su importancia, sumando un objetivo recomendado de **10 puntos diarios**.
2. **Configuración del Plan**: El plan sugerido se guarda en la base de datos de manera estructurada (Metas, Disciplinas y Puntos). El vendedor puede modificar, agregar o eliminar disciplinas y ajustar sus pesos.
3. **Registro de Consistencia Diario (Checklist)**: Cada día hable, el vendedor marca las actividades completadas. El sistema calcula los puntos obtenidos ese día y los acumula en el historial de consistencia.
4. **Dashboard del Coordinador (Auditoría y Alineación)**: El coordinador de la empresa define un presupuesto de facturación global. El sistema audita automáticamente si la suma de las metas de los vendedores cubre el objetivo de la empresa, calcula el rendimiento real (multiplicando cierres reales por ticket promedio) y ofrece recomendaciones de optimización por vendedor utilizando IA.

---

## 2. Especificación de Base de Datos (Esquema Relacional)

Para implementar el sistema, se requieren tres entidades principales o adiciones a tablas existentes:

### A. Adición a la tabla `Company` (Empresa)
* **`global_sales_target`** (Float, por defecto `0.0`): Almacena la meta de facturación mensual global de la empresa.
* **`global_goals`** (Text, nullable): Detalla las directrices estratégicas de la empresa.

### B. Tabla `SlightEdgePlan` (Plan de La Ventaja del Vendedor)
Representa la configuración del plan activo del vendedor.
* **`id`** (Integer, Primary Key)
* **`user_id`** (Integer, Foreign Key -> `users.id`, Unique)
* **`monthly_income_goal`** (Float, por defecto `0.0`): Meta de ingresos mensuales del vendedor.
* **`ticket_average`** (Float, por defecto `0.0`): Precio promedio de su producto/servicio.
* **`conversion_rate`** (Float, por defecto `0.0`): Tasa de conversión de prospecto/cita a cierre de venta.
* **`funnel_metrics`** (JSON, nullable): Desglose del embudo calculado (Llamadas -> Citas -> Cotizaciones -> Ventas).
* **`activities_config`** (JSON, non-null): Lista de disciplinas diarias configuradas. Formato:
  ```json
  [
    { "activity": "Hacer llamadas de prospección", "points": 2 },
    { "activity": "Generar citas de ventas", "points": 3 },
    { "activity": "Enviar cotizaciones", "points": 1 },
    { "activity": "Cierre de ventas", "points": 4 }
  ]
  ```
* **`daily_points_goal`** (Integer, por defecto `10`): Puntos diarios a lograr.

### C. Tabla `SlightEdgeLog` (Registro de Consistencia Diario)
Registra el avance diario del checklist.
* **`id`** (Integer, Primary Key)
* **`user_id`** (Integer, Foreign Key -> `users.id`)
* **`date`** (Date, non-null)
* **`completed_activities`** (JSON, non-null): Mapeo de actividades realizadas y cantidad. Formato:
  ```json
  {
    "Hacer llamadas de prospección": 5,
    "Generar citas de ventas": 2,
    "Cierre de ventas": 1
  }
  ```
* **`total_points`** (Integer, por defecto `0`): Suma de puntos obtenidos en el día aplicando los pesos del plan del vendedor.
* **Constraint**: Llave única compuesta (`user_id` + `date`).

---

## 3. Especificación de Endpoints del API Backend

### A. `GET /api/slight-edge/plan/{user_id}`
* **Descripción**: Retorna el plan configurado del vendedor.
* **Respuesta (200 OK)**:
  ```json
  {
    "monthly_income_goal": 50000.0,
    "ticket_average": 10000.0,
    "conversion_rate": 20.0,
    "activities_config": [
      { "activity": "Llamadas de prospección", "points": 2 }
    ],
    "daily_points_goal": 10
  }
  ```

### B. `POST /api/slight-edge/plan/{user_id}`
* **Descripción**: Crea o actualiza el plan de La Ventaja.
* **Payload**:
  ```json
  {
    "monthly_income_goal": 50000.0,
    "ticket_average": 10000.0,
    "conversion_rate": 20.0,
    "activities_config": [
      { "activity": "Llamadas de prospección", "points": 2 }
    ],
    "daily_points_goal": 10
  }
  ```

### C. `GET /api/slight-edge/log/{user_id}?date_str=YYYY-MM-DD`
* **Descripción**: Retorna el registro de un día específico. Si `date_str` se omite, retorna la lista de logs de los últimos 30 días para graficar el progreso histórico.

### D. `POST /api/slight-edge/log/{user_id}`
* **Descripción**: Guarda el checklist diario. El backend calcula y guarda automáticamente `total_points` basándose en los pesos vigentes de `activities_config`.
* **Payload**:
  ```json
  {
    "date_str": "2026-06-23",
    "completed_activities": {
      "Llamadas de prospección": 3
    }
  }
  ```

### E. `POST /api/slight-edge/coaching-chat/{user_id}`
* **Descripción**: Endpoint conversacional del Coach de IA. Proporciona el historial de chat anterior y el mensaje del usuario. El modelo debe tener disponible la función (tool call) `save_slight_edge_plan` para guardar automáticamente el plan estructurado una vez que el usuario acepte la propuesta.

### F. `GET /companies/{company_code}/dashboard`
* **Descripción**: Retorna las métricas agregadas y listado de rendimiento del equipo para el Panel Coordinador.
* **Cálculo de Métricas**:
  1. Obtiene todos los vendedores vinculados a la empresa.
  2. Para cada vendedor, consulta su `SlightEdgePlan` (Meta e Ingresos) y los `SlightEdgeLog` de los últimos 30 días.
  3. Ejecuta el algoritmo de **Fuzzy Matching** (ver Sección 5) para sumar las actividades reales (Llamadas, Citas, Cotizaciones, Ventas).
  4. Calcula:
     - `Ventas Reales Logradas = Conteo de Ventas completadas * ticket_average`
     - `Consistencia Diaria = Promedio de total_points en los logs de los últimos 30 días`
     - `Conversión Real = (Ventas Logradas / Citas Logradas) * 100` (Si Citas > 0, de lo contrario fallback a la conversión planificada).
* **Respuesta**:
  ```json
  {
    "global_goals": "Directrices de la empresa...",
    "global_sales_target": 150000.0,
    "company_name": "Nombre Empresa",
    "aggregated": {
      "total_sales": 120000.0,
      "total_target": 180000.0,
      "avg_conversion": 22.5,
      "avg_roi": 8.5
    },
    "sellers": [
      {
        "id": 1,
        "name": "Juan Perez",
        "role": "vendedor_empresa",
        "metrics": {
          "sales": 40000.0,
          "target": 50000.0,
          "conversion_rate": 20.0,
          "roi": 9.2
        },
        "slight_edge": {
          "planned_conversion_rate": 25.0
        }
      }
    ]
  }
  ```

### G. `POST /companies/{company_code}/sellers/{seller_id}/ai-goals`
* **Descripción**: Genera una auditoría y recomendación de IA comparativa para el coordinador. Utiliza el plan del vendedor y sus estadísticas reales de los últimos 30 días para enviarle un análisis detallado a Gemini.

---

## 4. Prompts de Inteligencia Artificial (Vertex AI / Gemini)

### A. Prompt del Chat de Coaching (Inicio del Proceso)
```
Eres un coach de ventas experto y empático. Estás iniciando una sesión estructurada para diseñar el plan de "La Ligera Ventaja" (Slight Edge) del vendedor.

Tu objetivo es guiarlo paso a paso:
1. Pregunta amigablemente por sus objetivos financieros mensuales (ingresos deseados).
2. Pregunta por su ticket promedio de venta.
3. Pregunta o estima su tasa de conversión actual de citas a cierres.
4. Con base en estos tres números, calcula el embudo inverso de ventas necesario para lograr el objetivo.
5. Diseña una propuesta diaria de disciplinas constantes (acciones cotidianas y sencillas como llamadas, seguimiento, prospección).
6. Asigna a cada acción un peso en puntos en función de su importancia de tal modo que al día sumen aproximadamente 10 puntos.

Una vez que el vendedor esté conforme con las actividades y los puntos, debes llamar obligatoriamente a la función local 'save_slight_edge_plan' enviándole los parámetros estructurados correspondientes para guardar su configuración de forma permanente.
```

### B. Prompt de Auditoría del Coordinador (Sugerencias de IA)
```
Eres un coach de ventas experto especializado en la metodología "La Ligera Ventaja" (The Slight Edge) de Jeff Olson.
La empresa '{company_name}' tiene las siguientes directrices y metas:
- Meta de Facturación Mensual Global de la Empresa: ${global_sales_target}
- Estrategia Global: {global_goals}

Analiza al vendedor '{seller_name}' con base en su plan de La Ventaja y su desempeño de los últimos 30 días:

PLAN ACTUAL DE LA VENTAJA:
- Meta de ingresos mensuales del vendedor: ${seller_target_income}
- Ticket promedio: ${seller_ticket}
- Tasa de conversión planificada: {seller_conversion_rate}%
- Meta diaria de puntos: {seller_daily_points} pts
- Disciplinas diarias configuradas en su plan: {seller_disciplines}

RENDIMIENTO REAL EN LOS ÚLTIMOS 30 DÍAS:
- Llamadas completadas: {actual_calls}
- Citas completadas: {actual_meetings}
- Cotizaciones completadas: {actual_quotes}
- Ventas reales logradas (cierres): {actual_sales} (Monto estimado: ${actual_sales_amount})
- Consistencia promedio diaria: {actual_avg_points} pts (Meta diaria: {seller_daily_points} pts)
- Días con registro de actividades: {logged_days} días

Genera una sugerencia de coaching concisa y accionable para este vendedor (máximo 3 párrafos cortos, en español).
Compara su desempeño real contra sus metas planificadas y sugiere si debe ajustar sus disciplinas, mejorar su consistencia diaria, o si sus metas están correctamente alineadas para lograr el éxito global de la empresa.
No uses saludos ni introducciones, responde directamente con la recomendación.
```

---

## 5. Algoritmo de Fuzzy Matching (Python)

Dado que las disciplinas diarias son editables por el vendedor, el sistema categoriza las actividades reales de los logs de forma dinámica utilizando coincidencias parciales de palabras clave en minúsculas:

```python
def categorize_activity(name: str) -> str:
    """
    Clasifica las disciplinas editables de los usuarios en 4 categorías estándar.
    """
    n = name.lower().strip()
    # 1. Llamadas / Prospección
    if any(x in n for x in ["llam", "call", "prospect", "contac"]):
        return "llamada"
    # 2. Citas / Reuniones
    if any(x in n for x in ["cit", "reun", "meet", "visita"]):
        return "cita"
    # 3. Cotizaciones / Propuestas
    if any(x in n for x in ["cotiz", "propuest", "presupuest", "quot", "enviar"]):
        return "cotizacion"
    # 4. Ventas / Cierres / Cobro
    if any(x in n for x in ["cierr", "vent", "cobro", "clos", "firm"]):
        return "venta"
    return "otra"
```

---

## 6. Lógica de Alineación de Metas en el Frontend (Javascript)

El Panel Coordinador calcula en el cliente la alineación de metas y renderiza estados visuales de acuerdo a la meta global de la empresa (`globalSalesTarget`) y la suma de metas individuales configuradas por el equipo (`sumSellersTarget`):

```javascript
// Set target global de facturación
const globalSalesTarget = data.global_sales_target || 0.0;
document.getElementById('coordinator_global_sales_target').value = globalSalesTarget || '';

// Calcular sumatoria de metas del equipo
let sumSellersTarget = 0;
coordinatorSellers.forEach(s => {
    sumSellersTarget += (s.metrics && s.metrics.target) ? s.metrics.target : 0;
});

const alignmentAlert = document.getElementById('coordinator_alignment_alert');
const alignmentIcon = document.getElementById('alignment_icon');
const alignmentTitle = document.getElementById('alignment_status_title');
const alignmentDesc = document.getElementById('alignment_status_desc');
const alignmentDiffVal = document.getElementById('alignment_diff_val');

if (globalSalesTarget <= 0) {
    // Estado: Sin Configurar
    alignmentAlert.style.display = 'flex';
    alignmentAlert.style.borderLeft = '4px solid var(--text-muted)';
    alignmentIcon.innerHTML = '<i class="ph ph-info" style="color:var(--text-muted);"></i>';
    alignmentTitle.textContent = 'Meta Global no Configurada';
    alignmentDesc.textContent = 'Define una meta de facturación mensual global para auditar la alineación de tu equipo.';
    alignmentDiffVal.textContent = '$0';
    alignmentDiffVal.style.color = 'var(--text-muted)';
} else if (sumSellersTarget >= globalSalesTarget) {
    // Estado: Metas Alineadas (Excedente)
    const diff = sumSellersTarget - globalSalesTarget;
    alignmentAlert.style.display = 'flex';
    alignmentAlert.style.borderLeft = '4px solid var(--success)';
    alignmentIcon.innerHTML = '<i class="ph-fill ph-check-circle" style="color:var(--success);"></i>';
    alignmentTitle.textContent = 'Metas Alineadas con la Empresa';
    alignmentDesc.textContent = '¡Excelente! La suma de las metas de La Ventaja de tus vendedores cubre la meta global.';
    alignmentDiffVal.textContent = '+$' + diff.toLocaleString();
    alignmentDiffVal.style.color = 'var(--success)';
} else {
    // Estado: Brecha / Déficit en metas del equipo
    const diff = globalSalesTarget - sumSellersTarget;
    alignmentAlert.style.display = 'flex';
    alignmentAlert.style.borderLeft = '4px solid var(--danger)';
    alignmentIcon.innerHTML = '<i class="ph-fill ph-x-circle" style="color:var(--danger);"></i>';
    alignmentTitle.textContent = 'Brecha en Metas del Equipo';
    alignmentDesc.textContent = 'La suma de las metas de La Ventaja de tus vendedores NO cubre la meta global de la empresa.';
    alignmentDiffVal.textContent = '-$' + diff.toLocaleString();
    alignmentDiffVal.style.color = 'var(--danger)';
}
```

---

## 7. Checklist del Desarrollador para Nuevos Proyectos

Al migrar este módulo a un nuevo codebase, asegúrate de verificar los siguientes puntos:
- [ ] Crear las migraciones de base de datos para `SlightEdgePlan` y `SlightEdgeLog` asociadas al ID de usuario.
- [ ] Añadir `global_sales_target` a la tabla de empresas o inquilinos (Tenants) para habilitar el banner de alineación.
- [ ] Configurar el cliente de IA (Gemini API / Vertex AI / OpenAI) e inyectar el prompt de coaching estructurado en español.
- [ ] Proveer al Coach de IA una función local para guardar la configuración final calculada (`monthly_income_goal`, `ticket_average`, `conversion_rate`, `activities_config`).
- [ ] Implementar la función de clasificación por *Fuzzy Matching* en el backend para generar las estadísticas agrupadas reales a partir de los registros de checklist.
- [ ] Modificar el panel del vendedor para que renderice el checklist interactivo diario y calcule el progreso en base al acumulado de puntos del día.
- [ ] Implementar la vista del coordinador con el gráfico comparativo (Ventas Reales vs Meta Planificada) y el contenedor de alertas de alineación.
