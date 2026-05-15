# Diario de Desarrollo - Ialienant 🐜

## Hito 0.1: Cimentación del Core y WebSockets - 05/04/2026
* **Estructura de Archivos:** Se determinó que los archivos fuente (`main.py`, `state.py`) deben residir en la raíz del módulo (`alienant-core/`) y **nunca** dentro de la carpeta `venv/`. Esto asegura compatibilidad con Git y previene la pérdida de código fuente.
* **Troubleshooting (Pylance):** Si VS Code no reconoce dependencias como `pydantic`, se debe forzar el intérprete (`Ctrl+Shift+P` -> `Python: Select Interpreter`) apuntando directamente al binario dentro de `venv/Scripts/python.exe`.
* **Importaciones en FastAPI:** Para la ejecución de desarrollo local con `uvicorn`, las importaciones internas (ej. `from state import ...`) deben ser absolutas respecto a la raíz del módulo para evitar el error `Could not import module`.
* **Manejo de "Dead Code":** Las advertencias de Pylance sobre importaciones no utilizadas (como las clases de estado en el Mock Orchestrator inicial) son esperadas en las fases tempranas de construcción antes de la integración total de LangGraph.

---

### Hito 0.2: Implementación de ConnectionManager y Robustez de Streaming - 07/04/2026
* **Arquitectura de Red:** Se migró la gestión de WebSockets de un manejo directo en `main.py` a un patrón de diseño **Manager (Singleton)** ubicado en `core/websocket_manager.py`. Esto desacopla la lógica de transporte de la definición de los endpoints.
* **Prevención de Memory Leaks:** Se implementó un ciclo de vida estricto para las conexiones (`connect` -> `try/finally` -> `disconnect`). Esto asegura que, ante cierres inesperados de VS Code o caídas de red, los recursos del servidor se liberen en tiempo real (Complejidad de limpieza **O(1)**).
* **Abstracción de Mensajería:** Se estandarizaron los métodos `send_personal_message` y `broadcast_telemetry`. Ahora el sistema es capaz de direccionar ráfagas de tokens (`TOKEN_CHUNK`) específicamente a la tarea (`task_id`) que las originó, permitiendo sesiones multi-tarea en el futuro sin colisión de datos.
* **Troubleshooting (Handshake):** Se identificó que el bloque `while True` en el endpoint es crítico para mantener el socket abierto; de lo contrario, FastAPI cierra la conexión al finalizar la función. Se añadió un manejo de excepción `WebSocketDisconnect` para silenciar errores de socket limpios en la terminal.

---

## Hito 0.3 & 0.4: Persistencia de Estado y Refactorización Enterprise - 08/04/2026
* **Persistencia Atómica (SQLite):** Se implementó el sistema de Checkpointing utilizando SqliteSaver. Ahora, el estado del grafo no solo reside en la RAM, sino que se guarda físicamente en checkpoints.db. Esto permite la recuperación de sesiones ante caídas del servidor y sienta las bases para el "Time-Travel Debugging" (Capacidad de volver a estados anteriores del hilo).
* **Gestión de Recursos (Anti-Leak):** Se introdujo el patrón Context Manager (with checkpoint_manager.get_saver()) para la apertura y cierre de conexiones a la base de datos. Esta arquitectura garantiza una limpieza de recursos O(1), eliminando cualquier riesgo de fugas de memoria por conexiones huérfanas.
* **Refactorización de Grafo (Factory Pattern):** Se migró la instanciación del grafo a una función fábrica (build_ailienant_graph). Se eliminaron todas las variables globales en graph.py, logrando un desacoplamiento total entre la definición de la topología y el motor de ejecución JIT (Just-In-Time).
* **Blindaje de Tipado (Pylance Strict):** Se resolvieron todas las ambigüedades de tipado estático mediante el uso de cast de Python para RunnableConfig y AilienantGraphState. El código ahora reporta 0 errores/advertencias en linters de nivel estricto, garantizando que el contrato de datos se cumpla en cada salto de nodo.
* **Optimización de Documentación:** Se depuró el código muerto (Dead Code) y se actualizaron los docstrings para reflejar la nueva arquitectura agnóstica de persistencia.

---

## Hito 0.5: Cerebro de Enrutamiento y Blindaje de Contexto - 09/04/2026
* **Matriz de Enrutamiento 3D (logic/routing_engine.py):** Se implementó el motor de decisión heurístico $O(M)$ que evalúa CSS (Contexto), TCI (Complejidad) y Capacidad (Hardware). Este nodo elimina la "ceguera de hardware" y previene errores de Out-of-Memory (OOM) mediante un buffer del 20% en la ventana de contexto.
* **Precisión de Tokenización (utils/token_counter.py):** Integración de `tiktoken` para el conteo quirúrgico de tokens. Esto permite al orquestador predecir si un prompt desbordará el modelo local antes de realizar la inferencia, optimizando el fallback a la nube.
* **Arquitectura de Carpetas (Clean Architecture):** Se migró el prototipo a una estructura modular (`logic/` y `utils/`). Se aplicó el principio de Separación de Preocupaciones (SoC), desacoplando la lógica de negocio de las herramientas de soporte.
* **Troubleshooting (PowerShell):** Resolución de conflictos de comandos Unix vs Windows mediante el uso de `New-Item -Force` para la creación recursiva de módulos Python (`__init__.py`).

---

## Hito 0.6: Orquestación Dinámica de Agentes y Blindaje de Permisos - 10/04/2026

* **Consolidación de Nodos Cognitivos (`core/agents/`):** Migración de 9 agentes estáticos a 5 Nodos Base dinámicos. Se implementó el mecanismo de **Prompt Swapping** que inyecta directivas de rol $R \in \{Refactor, Infra, Doc, SecOps, Test\}$ en tiempo de ejecución, reduciendo la carga cognitiva del modelo y optimizando el uso de la ventana de contexto.
* **Protocolo de Seguridad MCP (`core/permissions.py`):** Diseño del interceptor de privilegios con cuatro niveles de acceso granulares (**ReadOnly, Write, Execute, Dangerous**). Se estableció la validación determinista **Read-Before-Write (RBW)** para mitigar la corrupción accidental de archivos por parte de LLMs locales.
* **Estructura del Estado Neuronal (`core/state.py`):** Definición del `IalienantGraphState` utilizando `Annotated` y reductores de LangGraph. El esquema gestiona el `wbs_plan`, el mapeo de archivos leídos y el `retry_count`, garantizando la persistencia del hilo de pensamiento y la prevención de bucles infinitos en el Micro-Enjambre de QA.

## hito 1.0.0📅 [13/04/2026] | Sesión de Desarrollo: Cierre de la Fase 0 (Infraestructura Core)

### 🚀 Resumen de Logros
Finalización exitosa de los cimientos técnicos de **AILIENANT**. La infraestructura base es ahora resiliente, fuertemente tipada y preparada para la orquestación de agentes.

### 🛠️ Detalles Técnicos de la Sesión

* **Motor de Red Optimizado ($O(1)$):**
    * Se implementó `TypeAdapter` de **Pydantic V2** en `websocket_manager.py`.
    * Logro: Validación instantánea de Uniones Discriminadas, asegurando que solo los eventos que cumplen los contratos lleguen al sistema.
* **Entrypoint Resiliente (FastAPI):**
    * Construcción de `main.py` con manejo de ciclo de vida de WebSockets.
    * Logro: Implementación de bloques `try-except WebSocketDisconnect` para garantizar **Zero Memory Leaks** ante desconexiones abruptas del IDE.
* **Persistencia de Estado (HITL Ready):**
    * Configuración de `engine.py` utilizando `SqliteSaver`.
    * Logro: Conexión de base de datos local para habilitar la memoria a largo plazo de LangGraph y permitir pausas en el flujo para intervención humana (Human-in-the-loop).
* **Puerta de Enlace LLM (Factory Pattern):**
    * Desarrollo de `llm_gateway.py` para abstracción de modelos.
    * Logro: Enrutamiento dinámico entre **Ollama** (local) y **OpenAI** (nube) con `temperature=0.0` para garantizar respuestas deterministas en tareas de ingeniería.
* **Contratos REST VFS-Ready:**
    * Creación del endpoint `POST /task/submit` con soporte para `dirty_buffers`.
    * Logro: El sistema ahora puede sincronizar archivos modificados no guardados en el IDE antes de iniciar cualquier misión de IA.
* **Seguridad, RBAC y XML Sandboxing:**
    * Implementación de `rbac.py` y `prompts.py`.
    * Logro: Transición a **4 Nodos de Poder** (Planner, Orchestrator, Logic, Analyst) y mitigación de inyecciones de prompt mediante etiquetas `<file_content>` delimitadas.

### 🧪 Validación de Calidad (QA)
- [x] **Prueba REST:** Endpoint `/task/submit` validado vía Swagger UI con payloads complejos.
- [x] **Prueba WS:** Script de prueba `qa_ws.py` confirmó que el firewall rechaza paquetes malformados y procesa eventos válidos.
- [x] **Estabilidad:** Cero errores de enrutamiento y gestión de puertos 8000 estable.

## 🚀 HITO 1.0.1 📅 [15/04/2026] | Estabilización de Arquitectura y VFS

### Logros Técnicos:
* **Tipado Estricto Resuelto:** Se solucionó la colisión entre el patrón Singleton (`__new__`) y Pylance/LSP mediante la declaración de la anotación `_ram_vfs: Dict[str, str]` a nivel de clase, garantizando autocompletado y validación estática sin errores de "member not defined".
* **Inversión de Dependencias (SRP):** Se implementó la capa `core/task_service.py`. Esta capa actúa como el orquestador de lógica de negocio, aislando con éxito la lógica cognitiva y el manejo del VFS de los controladores de transporte en `main.py`.
* **Unificación del API Gateway:** Refactorización integral de `main.py`. Se consolidaron las rutas HTTP y el túnel de WebSockets bajo un esquema de enrutamiento profesional y versionado (`/api/v1/`), eliminando endpoints redundantes y preparando el sistema para producción.
* **Testing de Integración VFS:** Ejecución exitosa de `test_vfs.py`. Se validó empíricamente que el middleware actúa como un proxy de lectura:
    * **Fallback:** Lectura de disco duro cuando no hay cambios.
    * **Interceptación:** Retorno inmediato $O(1)$ desde RAM cuando existen "dirty buffers" (entropía del IDE), evitando I/O innecesario.
* **Refactorización Mayor (WBS Fase 2):** Limpieza profunda de la hoja de ruta. 
    * Sustitución de `networkx` por **LangGraph** (`StateGraph`) para la orquestación de agentes.
    * Implementación de **SQLite WAL Mode** para permitir concurrencia segura entre los Checkpoints de la IA y el servidor API.
    * Eliminación de lecturas directas `os.open` en favor del `VFSMiddleware`.

## 🚀 HITO 1.0.2 📅 [13/05/2026] | Handshake Bidireccional, Soberanía de Modelos y Motor AST

### 🚀 Resumen de Logros
Esta sesión marca la transición de AILIENANT de una infraestructura pasiva a un sistema **consciente de la sintaxis y soberano**. Se cerró el ciclo de comunicación bidireccional entre el IDE y el Backend, y se implementó la base para la edición de código a prueba de errores mediante árboles de sintaxis (AST).

### 🛠️ Detalles Técnicos de la Sesión

* **Handshake de Intención y UI (Phase 1.4.1):**
    * Implementación de un **Webview optimizado** en `App.tsx` usando Vanilla TypeScript y un bundle IIFE vía `esbuild`.
    * Logro: Creación del "Planner Mode Toggle" con estilos nativos de VS Code. Comunicación bidireccional establecida: UI -> Extension -> WebSocket -> Backend.
* **Control de Concurrencia Optimista (OCC - Phase 1.5):**
    * Interceptación de mutaciones mediante validación de `document.version`.
    * Logro: Protección contra el "Efecto Fantasma". El sistema ahora detecta y bloquea intentos de parcheo si el usuario modificó el archivo durante la inferencia de la IA, emitiendo un evento `client_concurrency_conflict`.
* **Gateway Soberano y Autodescubrimiento (Phase 1.6):**
    * Integración de **LiteLLM Proxy** como intermediario absoluto (`localhost:4000`).
    * Logro: Implementación de `config_generator.py` con escaneo asíncrono de puertos (Ollama, LM Studio). Nuevo endpoint `GET /api/v1/models/available` que permite a la extensión conocer en tiempo real los modelos locales y de nube disponibles.
* **Motor AST Multilingüe (Tree-sitter 0.25):**
    * Integración de un motor de análisis sintáctico en `core/ast_engine.py` compatible con Python 3.13.
    * Logro: Soporte para 29 lenguajes mediante parsers individuales. El VFS ahora genera y cachea representaciones AST con una política de **Lazy Loading**, permitiendo a la IA "entender" la estructura lógica (nodos, clases, funciones) en lugar de solo texto plano.
* **Persistencia de Auditoría y Catálogo (SQLite WAL):**
    * Creación de `ailienant_catalog.sqlite` con tablas para `session_state` y `tool_registry`.
    * Logro: Implementación del protocolo **Read-Before-Write (RBW)**. Cada lectura de archivo queda registrada, creando una bitácora de auditoría que previene alucinaciones sobre archivos no consultados previamente por el agente.

### 🧪 Validación de Calidad (QA)
- [x] **Prueba de Compilación:** `npm run compile` exitoso con 0 errores de tipos en el bundle del Webview.
- [x] **Prueba AST:** Validación de `root_node.type == "module"` en archivos Python y gestión de caché por hash de contenido exitosa.
- [x] **Prueba de Persistencia:** Verificación de inserción en `tool_registry` y persistencia de logs de sesión tras reinicio del servidor en modo WAL.
- [x] **Prueba OCC:** Bloqueo confirmado de mutaciones al simular desfase de versión entre el IDE y el Backend.

---

## 🚀 HITO 1.0.3 📅 [13/05/2026] | Anti-Entropía, Sostenibilidad de Contexto y Blindaje de Runtime

### 🚀 Resumen de Logros
Esta sesión consolidó la estabilidad industrial de **AILIENANT**. Se implementó un sistema de "salud sistémica" que previene el desbordamiento de memoria por contexto, blinda la integridad de la base de datos ante cierres abruptos y establece un control de flujo elástico para la comunicación con el IDE.

### 🛠️ Detalles Técnicos de la Sesión

* **Compresión de Estado y Ventana Deslizante (Phase 2.1.11):**
    * Implementación del nodo `StateSummarizer` en LangGraph con un umbral del 80% de la ventana de contexto.
    * Logro: Uso del **Modelo Small (1.5B)** para condensar el historial antiguo en un `SystemSummaryMessage`, manteniendo intactos los últimos 5 turnos (Cognitive Horizon). Prevención de errores *Context OOM*.
* **Debouncing de I/O y Coalescencia de Eventos (Phase 2.1.12):**
    * Creación de `core/io_coalescer.py` con una ventana de 500ms para actualizaciones de archivos.
    * Logro: Reducción masiva de carga en CPU/Disco al agrupar múltiples *Save Hooks* (ej. Prettier formatting) en un solo lote de indexación AST y PPR, evitando saturación del WAL de SQLite.
* **Gestión de Branch Switching y Poda de Grafo (Phase 2.1.13):**
    * Implementación de **Dynamic Thresholding** (>100 archivos) para desviar indexaciones masivas a workers de baja prioridad.
    * Logro: Protocolo **Unlink-First**. Las eliminaciones se procesan antes que las creaciones, purgando nodos huérfanos y "fantasmas" de dependencias obsoletas para erradicar alucinaciones de navegación.
* **Guardrails de Integridad y Auto-Corrección (Phase 2.1.14):**
    * Introducción del nodo `OutputGuardrailNode` con validación Pydantic estricta.
    * Logro: Bucle cerrado de reintento (Max 2) para modelos locales. Si el JSON o el código vienen malformados, el sistema genera feedback automático al LLM para auto-corrección antes de impactar el estado.
* **Arquitectura de Estado Sombrío (Shallow State) y CAS (Phase 2.1.x):**
    * Refactorización del VFS para sustituir `content: str` por `blob_hash: str` (Blake2b).
    * Logro: Implementación de `core/blob_storage.py` (Content-Addressable Storage). El estado del grafo ahora es "ligero" (hashes), mientras que los archivos pesados residen en un almacén de blobs, reduciendo el costo de serialización en un 99%.
* **Backpressure y Seguridad de Persistencia (Anti-Entropy):**
    * Implementación de `transport/throttler.py` para monitorear el buffer de escritura del WebSocket.
    * Logro: El streaming de tokens del LLM se pausa automáticamente si el IDE no consume datos. Adicionalmente, se aseguró el cierre limpio mediante `PRAGMA wal_checkpoint(TRUNCATE)` en el shutdown hook del servidor.

### 🧪 Validación de Calidad (QA)
- [x] **Pruebas de Infraestructura:** 16 nuevos tests DoD aprobados (coalescencia, compresión y reductor de mensajes).
- [x] **Pruebas de Integridad:** 9 tests de guardrails y branch-switch exitosos.
- [x] **Regresión:** Los 24 tests de enrutamiento originales mantienen 100% de éxito.
- [x] **Análisis Estático:** `mypy` reporta 0 errores en los 8 nuevos archivos de soporte.

---

## 🚀 HITO 1.0.4 📅 [13/05/2026] | Adaptador Transparente MCP y FinOps de LangGraph, Planner Orchestration y Swarms de Ejecución y Socratic Ideation & HITL Suspension 

* **Arquitectura FinOps (Seguridad de Costos):** Implementamos un nodo `finops_gate` en `brain/engine.py` que intercepta la ejecución antes de aplicar parches (`apply_patch`). Para evitar *race conditions* en la ejecución concurrente de agentes, el `current_cost_usd` en `brain/state.py` utiliza `Annotated[float, operator.add]`.
* **Aislamiento de I/O en MCP:** Se creó `McpToolAdapter` en `tools/mcp_adapter.py`. Toda llamada externa ahora está protegida por `asyncio.wait_for`. Esto previene bloqueos indefinidos en el Event Loop de FastAPI si un servidor MCP externo (ej. análisis de dependencias) no responde.
* **Inyección de Dependencias de Tools:** Desarrollamos un patrón de registro `McpToolRegistry` que filtra inyecciones hacia `llm.bind_tools()` basado en el enum `AgentRole` (PLANNER/CODER/ANALYST/ORCHESTRATOR). Esto asegura que no contaminemos la ventana de contexto del LLM con herramientas que su rol no requiere (Context Sufficiency Score optimizado).
* **QA:** Alcanzamos 73/73 tests de regresión exitosos en pytest.
* **Streaming y Seguridad de Memoria en FastAPI:** Aislamos la ejecución del grafo migrando `alienant_app.astream()` a `TaskService.process_task`. Para el streaming de la UI (`vfs_manager.broadcast_token`), implementamos un patrón de referencias fuertes (`_background_tasks = set()`). Esto previene el *Garbage Collection Hazard*, evitando que Python destruya los mensajes de WebSockets en pleno vuelo.
* **Inyección de Dependencias en Tools (VFS Sandbox):** Creamos `tools/agent_tools.py`. En lugar de pasar instancias de servicios globales, desarrollamos *Factories* (closures) como `make_read_file_tool(vfs_read)`. Esto garantiza que el LLM solo vea los argumentos estrictos (`path`, `content`) en su *Tool Schema*, blindando el acceso no autorizado al sistema operativo.
* **Arquitectura Map-Reduce para Swarms:** Se implementaron *stubs* para `LogicAgent` y `AnalystAgent`. El orquestador ahora es capaz de disparar agentes paralelos devolviendo el costo local (`current_cost_usd: 0.0`), que nuestro reductor `operator.add` consolida de forma segura sin colisiones de estado.
* **QA:** Alcanzamos 79/79 pruebas de regresión exitosas.
* **Grill Me Pattern:** El AnalystAgent ahora cuestiona el plan del usuario antes de ejecutarlo. Se implementó una lógica de "Recomendación" para reducir la fricción del usuario.
* **Non-Blocking Persistence:** Se resolvió el reto de la espera humana usando un grafo que se suspende (`hitl_pending`) y se reanuda mediante LangGraph Checkpoints, evitando bloqueos de hilos en FastAPI.
* **Synthesis:** El nodo de síntesis comprime el diálogo en una especificación técnica inmutable para el resto de agentes.

---


## 🚀 HITO 1.0.5 📅 [14/05/2026] | Motor de Parcheo Atómico Determinista, Context Anchoring y AST Guard, VFS Transaccional y Puente IPC, Resiliencia del Grafo y Protección Políglota, Observabilidad y Auditoría Forense

### Motor de Parcheo Atómico Determinista
* **Prevención de Full-File Rewrites:** Se implementó `core/patcher.py` como un motor de reemplazo basado en el patrón SEARCH/REPLACE. Esto evita que el LLM regenere archivos completos, ahorrando miles de tokens de salida ($O(N)$ en facturación y latencia) y eliminando el riesgo de truncamiento de código.
* **Algoritmo de Dos Pasadas y Fallbacks:** El motor busca primero una coincidencia exacta. Si falla debido a problemas de identación o saltos de línea del LLM, normaliza los espacios en blanco (`\r\n` a `\n`) e intenta de nuevo.
* **Protección contra Ambigüedad:** Si el bloque de búsqueda aparece más de una vez en el archivo, el sistema lanza un `PatchError` explícito. Se prioriza el fallo seguro sobre una mutación arriesgada en el lugar equivocado.
### Context Anchoring y AST Guard 
* **Defensa contra la Ambigüedad:** Implementamos un validador en Pydantic que rechaza anclas (`search_block`) menores a 10 caracteres. Esto previene que el LLM intente parchear variables genéricas (ej. `i = 0`) que causarían fallos de múltiples coincidencias.
* **Fuzzy Fallback de Ventana Deslizante:** Para lidiar con alucinaciones de espacios en blanco o errores tipográficos menores del LLM, implementamos un algoritmo que usa `difflib.SequenceMatcher` evaluando el archivo por ventanas del mismo tamaño que el bloque de búsqueda. Se exige un *ratio* de similitud > 0.90 para proceder.
* **AST Sync Guard:** Antes de volcar el parche al VFS, si el archivo es `.py`, se compila en memoria con `ast.parse()`. Esto actúa como un "Fail-Fast", bloqueando instantáneamente cualquier parche que deje paréntesis huérfanos o identación corrupta.
### VFS Transaccional y Puente IPC 
* **OCC (Optimistic Concurrency Control):** Protegimos el VFS contra condiciones de carrera humano-IA. Se implementó una verificación de hashes (`expected_hash`); si el usuario modifica el archivo en VS Code mientras LangGraph procesa el parche, el motor lanza `StaleFileException`, forzando al LLM a re-leer el archivo y auto-corregirse sin romper el grafo.
* **Unified Diff & IPC:** En lugar de reescribir discos, el motor genera un Diff Unificado estándar en RAM y lo emite vía WebSocket (`server_vfs_patch_approved`). Esto delega la responsabilidad de escritura a la API nativa de VS Code (`WorkspaceEdit`), manteniendo intacto el historial de `Ctrl+Z` del usuario.
### Resiliencia del Grafo y Protección Políglota
* **Self-Correction Loop:** Se modificó la herramienta de parcheo para capturar `PatchError` y devolverlo como un string. Esto evita que LangGraph dispare un `ToolException` fatal, permitiendo que el Agente use su `observation` para corregir la sintaxis y reintentar de forma autónoma.
* **FinOps Telemetry:** Se implementó una heurística de $O(1)$ (`len // 4`) para estimar y loguear los tokens de salida ahorrados en cada parche exitoso.
* **Surgical Strike Protocol:** Para evitar la corrupción de archivos de sintaxis mixta (ej. `.blade.php`, `.vue`, `.tsx`), se implementó `is_polyglot_file()`. El `PlannerAgent` intercepta estos archivos e inyecta dinámicamente un constraint inmutable (usando `model_copy` de Pydantic) que prohíbe las reescrituras de archivo completo, forzando el uso exclusivo del `patch_tool`.
### Observabilidad y Auditoría Forense
* **Black-Box Recorder:** Se implementó un sistema de telemetría local persistente en SQLite (`telemetry.sqlite`). A diferencia de los logs de texto, esto permite realizar consultas analíticas sobre el comportamiento del agente.
* **Métricas de Decisión:** Cada vez que el Grafo toma una bifurcación, se capturan las métricas crudas (CSS/TCI) y la lógica de negocio (ej. "budget_rejected"). 
* **Arquitectura Thread-Safe:** El uso de `WAL mode` y `threading.Lock` garantiza que, incluso en ejecuciones paralelas (Swarm Mode), la telemetría no se corrompa ni ralentice el flujo principal del agente.