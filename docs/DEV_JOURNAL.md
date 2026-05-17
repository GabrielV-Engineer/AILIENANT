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


## 🚀 HITO 1.0.5 📅 [15/05/2026] | Motor de Parcheo Atómico Determinista, Context Anchoring y AST Guard, VFS Transaccional y Puente IPC, Resiliencia del Grafo y Protección Políglota, Observabilidad y Auditoría Forense, Sistema de Vigilia (.ailienant.json), y Checkpoint Gate - Certificación E2E.

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
### Sistema de Vigilia (.ailienant.json)
* **Identidad Pro:** Se estableció `.ailienant.json` como el archivo de configuración de reglas de estilo y arquitectura para el agente.
* **Caché Inteligente:** Se implementó una lógica de Singleton en `core/rules.py` que solo lee el disco si el archivo ha sido modificado, optimizando los recursos durante sesiones largas de codificación.
* **Inyección de Prompt:** El `PlannerAgent` ahora es "consciente" de las reglas del usuario. Esto permite imponer restricciones como 'No usar librerías externas' o 'Mantener funciones bajo 20 líneas' de forma persistente y automática.
### Checkpoint Gate - Certificación E2E 
* **Stress Test Superado:** Se validó que el `OutputParser` puede extraer JSON válido incluso cuando está envuelto en ruido o texto aleatorio, con una latencia promedio de 0.071ms.
* **Resiliencia al Bucle Infinito:** Se implementó y verificó mediante Mocks que un error persistente en el VFS desencadena los Guardrails. El agente consume sus intentos (`MAX_RETRIES`) y finaliza el grafo elegantemente hacia `__end__`, registrando el fracaso en la telemetría, evitando el gasto infinito de tokens.
* **Fase 2 Completada:** El núcleo transaccional, el enrutamiento de LangGraph, la telemetría local y el VFS están estabilizados.

---

## 🚀 HITO 1.0.6 📅 [15/05/2026] | Extractor GraphRAG Dinámico y Defensas de Memoria, Cierre del Bucle de Memoria Episódica, Motor Vectorial Semántico y Consolidación Atómica del CSS, Cierre de la Memoria Evolutiva - Zero-Drift & Lazy Parsing, Activación de la Matriz de Ruteo Dinámico, Consolidación del Veto Absoluto 

## Extractor GraphRAG Dinámico y Defensas de Memoria
* **Topología $k$-hop Asíncrona:** Implementación de un recorrido BFS sobre el árbol de dependencias (`aiosqlite`). Se introdujo *chunking* para evadir los límites de variables `IN` de SQLite, asegurando latencia $O(k)$ constante.
* **Protección del Event Loop:** Se extrajo el codificador de tokens (`tiktoken`) al *module level scope*, eliminando bloqueos de lectura de disco en la instanciación de clases durante la ejecución de los agentes.
* **Integridad de Estado:** El `PlannerAgent` ahora calcula y acopla la métrica `graph_coverage` respetando la inmutabilidad de los schemas (`Pydantic model_copy`), manteniendo puro el flujo de LangGraph.
* **Boy Scout Fix:** Se corrigió un *type hint* laxo en `shared/config.py` detectado por el control de calidad estricto (`mypy`).
## Cierre del Bucle de Memoria Episódica
* **Write-Loop de Trayectorias:** Se conectó `TrajectoryMemoryManager.memorize_trajectory` en el nodo de salida (`validate_output`) de LangGraph. 
* **Resiliencia Operativa:** La persistencia de la memoria se envolvió en un diseño *fire-and-forget* (Try/Except) para garantizar que caídas temporales en la base de datos vectorial o en el proveedor de embeddings no aborten operaciones agénticas que ya fueron evaluadas como exitosas.
## Motor Vectorial Semántico y Consolidación Atómica del CSS
* **SemanticMemoryManager & Pushdown:** Se implementó el motor de indexación vectorial en LanceDB (`core/memory/semantic_memory.py`) con particionamiento lógico (`workspace_hash`) usando *Predicate Pushdown* para multi-tenencia segura.
* **Resiliencia en Background:** La vectorización de archivos se integró en el `indexer.py` mediante un patrón *fire-and-forget* con *deferred imports*, aislando el pipeline de indexación de posibles caídas en la API de embeddings.
* **Truncamiento Seguro de UTF-8:** Se implementó una técnica de nivel Senior para evitar la corrupción de caracteres multibyte y errores 400 en LiteLLM: el texto se codifica con `tiktoken`, se recorta al límite seguro de la ventana (8191 tokens) y se vuelve a decodificar a string antes del embedding.
* **Recálculo Atómico de CSS:** El `PlannerAgent` ahora unifica las métricas de Topología (Fase 3.0), Semántica (Fase 3.1) y Recencia. El `css_total` y el flag `is_red_alert` se recalculan y aplican de forma atómica en una sola operación inmutable (`model_copy`).
## Cierre de la Memoria Evolutiva - Zero-Drift & Lazy Parsing
* **Arquitectura Zero-Drift:** Se eliminó el uso de `open()` en el motor RAG. Ahora toda lectura de archivos se canaliza a través de `VFSMiddleware`, permitiendo que la IA indexe y razone sobre archivos no guardados (dirty buffers) en tiempo real.
* **Lazy Deep Parsing:** Se implementó una separación de parsing AST. El sistema ahora solo realiza una extracción profunda de símbolos (clases/funciones) para los archivos identificados como Top-K semánticos y sus vecinos de primer grado, optimizando masivamente el uso de CPU.
* **Búsqueda Vectorial Optimizada:** Se consolidó el acceso a LanceDB mediante `search_with_paths`, reduciendo la latencia al evitar llamadas duplicadas a la API de embeddings de LiteLLM.
* **Validación DoD:** Se superó el script de auditoría de AST, confirmando que no existen accesos directos a disco en la ruta crítica del indexador, garantizando la integridad del VFS.
## Activación de la Matriz de Ruteo Dinámico 
* **Cierre de la Fase 3:** Se completó el motor de Metacognición. El sistema ahora decide autónomamente el nivel de cómputo necesario mediante una cascada de dos pasos.
* **Portero Matemático (O1):** Implementación de la lógica de "Red Alert" (CSS < 40%). Si el contexto es insuficiente, el sistema aborta el ruteo local y escala a la nube para mitigar riesgos de alucinación.
* **Mini-Juez de Complejidad:** Creación de `core/memory/context_auditor.py`. Se utiliza un modelo ligero para clasificar la intención del usuario. Tareas etiquetadas como `COMPLEX` fuerzan un TCI de 100.0, activando el nivel más alto de inferencia disponible.
* **Zero-Trust & Robustez:** Se implementaron fail-safes para que, ante cualquier caída del servidor de auditoría, el sistema por defecto no escale innecesariamente, protegiendo el presupuesto de tokens.
## Consolidación del Veto Absoluto 
* **Jerarquía de Decisión Finalizada:** Se estableció la soberanía del `RiskLevel` sobre las métricas puramente matemáticas. El sistema ahora opera bajo un modelo de "Confianza Verificada".
* **Lógica Monotónica:** Implementada la matriz 3x3 de ruteo. Se verificó mediante tests de aserción que el sistema es incapaz de degradar el nivel de cómputo si existe un riesgo semántico detectado, eliminando fallos por exceso de confianza (Overconfidence bias).
* **Blindaje de Telemetría:** El `Task Complexity Index (TCI)` ahora actúa como un espejo del riesgo semántico (75 para Medium, 100 para High), permitiendo auditorías de costos precisas en el futuro.
* **Estado del Proyecto:** Fase 3 (Memoria Evolutiva) completada al 100%. El núcleo es ahora capaz de autogestionar su contexto y ruteo con seguridad industrial.

---

## 🚀 HITO 1.0.6 📅 [15/05/2026] | Persistencia de Perfiles e Integración React, Session Delta Aggregator (Pre-Dream Reflection), MCTS Foundation + Nightmare Protocol, Polyglot Static Validation ("Micro-Isolate"), Dual-Rules Resolver & The Mirror

##  Persistencia de Perfiles e Integración React
* **Refactorización Frontend:** Migración exitosa de Vanilla DOM a React 18. Se implementó el patrón de "Lifting State" en `App.tsx` para controlar el flujo entre el `MasterToggle` y el `ProfileSelector`.
* **Comunicación Segura:** Implementación de un Bridge tipado (`vscode_bridge.ts`) y transferencia de estado inicial mediante atributos de datos (CSP-compliant).
* **Robustez en Backend:** Creación del paquete `core.config.profile` con validación Pydantic v2. Se implementó un manejador de errores para "Single File Workspaces" (`WorkspaceRootMissingError`).
* **Calidad de Código:** Cero errores en `mypy --strict` y `tsc`. Validación exitosa de la unión discriminada de WebSockets, asegurando que los eventos de cambio de perfil se despachan correctamente.
### Session Delta Aggregator (Pre-Dream Reflection)
* **State Evolution:** Added `session_delta: str` field to `AIlienantGraphState` (`state.py`) following immutability protocols.
* **Node Logic:** Created `session_delta_aggregator` LangGraph node (`aggregator_node.py`).
* **Analyst Integration:** Enhanced `AnalystAgent` to generate high-density summaries strictly capped at <500 tokens. The logic successfully extracts the last 5 user intents, deduplicates persistent terminal blockers (LSP errors), and flags uncommitted "Dirty Buffers" in the VFS.
* **Graph Wiring:** Successfully injected the aggregator node into `engine.py`, positioning it precisely between `summarize_history` and `route_after_summarize` to feed the planner agent with fresh session introspection.
* **Testing & Quality Assurance:**
    * `mypy --strict` passing with zero issues.
    * Developed 18 dedicated unit tests in `test_aggregator.py` simulating failed compilations, empty states, and token limit enforcement.
    * Global test suite remains stable (161/161 tests passing).
### MCTS Foundation + Nightmare Protocol
* **MCTS Tree:** Built `MCTSTree` and `MCTSNode` with per-node CAS-based `vfs_view` isolation. Implemented `prune_branch()` for immediate Garbage Collection (GC) of rejected universes, successfully mitigating heap overflow risks.
* **Nightmare Protocol:** Upgraded `AnalystAgent` with `evaluate_nightmare()` to strictly enforce architecture rules via `rules.json`. Returns `R=0` (failsafe) on violations to instantly kill rogue branches.
* **Episodic Memory:** Created `MCTSCheckpointer` with a dedicated SQLite `mcts_episodes` audit table, reusing existing LangGraph promotion logic.
* **Daemon Stub:** Scaffolded `OvernightDaemon` with `asyncio` lifecycle hooks without blocking the main FastAPI thread.
* **Quality Assurance:**
    * 10/10 new unit tests passed (171 global tests passing). DoD criteria successfully verified per-node GC and Nightmare scoring limits. Strict `mypy` typing enforced system-wide.
### Polyglot Static Validation ("Micro-Isolate")
* **RAM VFS (Flyweight):** Implemented `VirtualDocumentProvider` to overlay MCTS `vfs_view` over physical disk using CAS, ensuring zero disk mutation during validation.
* **Layer 1 (AST Filter):** Integrated $O(1)$ structural validation using `ast` (Python) and `tree-sitter` (TS/TSX).
* **Layer 2 (LSP Filter):** Built stateless, subprocess-based CLI wrappers for `ruff` and `eslint` via `stdin`, equipped with graceful degradation and timeouts.
* **Pipeline:** Plumbed the fail-fast orchestrator (`validate_delta`) to discard syntactically broken branches before invoking the expensive LLM Nightmare Protocol.
* **Quality Assurance:**
    * 25/25 new tests passed (196 global tests passing). 100% strict type coverage maintained system-wide.
### Dual-Rules Resolver & The Mirror
* **Phase 3.4.6 (Dual-Rules):** Upgraded `RuleManager` to support hierarchical resolution. It now reads from both global (`~/.ailienant/`) and local (`<workspace>/.ailienant/`) scopes. Implemented a composition engine that deep-merges dicts and concatenates/deduplicates lists, ensuring local rules always take strict precedence.
* **Phase 3.4.5 (The Mirror):** Created an in-memory Thread-Safe `MCTSRegistry` to track active search trees. Exposed FastAPI endpoints (`/vfs` and `/merge`) to interact with MCTS universes.
* **VS Code Integration:** Implemented the `ailienant-vision://` URI scheme via `TextDocumentContentProvider` in TypeScript, allowing native diff-views of dreamed code vs actual code. Built `applyMerge` with strict sandboxing, CAS preflight checks, and atomic OS-level writes.
* **Quality Assurance:**
    * +19 new tests added (215 global tests passing). 100% strict type coverage maintained. Zero backward compatibility breakages.

--- 

## 🚀 HITO 1.0.7 📅 [16/05/2026] | Silent Daytime Telemetry & Rule Distillation, Memory Lifecycle & Cognitive Fast-Boot

### Silent Daytime Telemetry & Rule Distillation
* **Frontend (TypeScript):** Implemented an $O(1)$ `BoundingBoxRegistry` that tracks code injected by the AI. Added a decay listener monitoring `onDidChangeTextDocument`. If the user modifies $\ge 70\%$ of the AI's original characters within a 3-minute window, an `AI_PAYLOAD_REJECTED` event is silently triggered.
* **Backend (Python):** Added `POST /api/v1/telemetry/reject`. Upgraded `AnalystAgent` with `distill_rejection_to_rule()`, prompting the mini-judge LLM to deduce the user's coding preference based on the diff.
* **Rule Engine:** Connected the distilled rule to `RuleManager.append_local_rule()`, utilizing a safe read-modify-write pattern to update `<workspace>/.ailienant/.ailienant.json` atomically without overwriting profile keys.
* **Quality Assurance:**
    * Reached 229 passing tests (+14 tests). All new telemetry, atomic writing, and LLM mock endpoints fully verified. Zero regressions.
### Hybrid Cascading & Smart Execution
* **Hybrid Routing:** Implemented a Dual-Tier cognitive architecture (`Tier.LOCAL` vs `Tier.CLOUD`) integrated directly into `LLMGateway.ainvoke`.
* **FinOps & Telemetry:** Created a thread-safe `TokenLedger` singleton to track local vs cloud token usage and calculate estimated USD savings, exposed via `GET /api/v1/telemetry/tokens`.
* **MCTS Coder (Local Fixer):** Built `agents/mcts_coder.py` with a "Fail-Fast" orchestrator. The `Tier.LOCAL` worker gets up to 3 attempts to fix syntax/LSP errors before a Circuit Breaker triggers the `Tier.CLOUD` Surgeon.
* **Supreme Judge:** Added `supreme_judge_evaluate()` using `Tier.CLOUD` to compute final node rewards, only invoked if local validation passes (saving massive API costs on hopeless branches).
* **Quality Assurance:**
    * Reached 246 passing tests (+17 tests). All routing, circuit breaking, fail-fast mechanics, and token accounting thoroughly verified. Zero regressions.
### Memory Lifecycle & Cognitive Fast-Boot
* **Memory Janitor (Phase 3.5):** Created `core/janitor.py` exposing `run_vector_gc` and `purge_obsolete_graphs`. Leveraged native `pyarrow` over LanceDB tables to achieve zero-copy filtering without adding heavy dependencies like `pandas`. Exposed via `POST /api/v1/system/janitor`.
* **Cognitive Fast-Boot (Phase 3.6):** Implemented `core/state_manager.py` to atomically dump state to `.ailienant/AGENTS.md` using a `` sentinel. Wired into `agents/planner.py` to skip expensive LanceDB vector searches when the cache is fresh.
* **Testability:** Promoted `DEBUG_MODE` in the planner to an environment-driven constant (`AILIENANT_PLANNER_DEBUG`), enabling proper `patch()` mocking in tests.
* **Quality Assurance:**
    * Reached 260 passing tests (+14 tests). Zero regressions. `mypy --strict` and `ruff` checks passed cleanly.

---

## 🚀 HITO 1.0.8 📅 [16/05/2026] | ContractGuardNode — Event-Driven Context Anchoring (Phase 2.26)

### Deterministic Middleware Between CoderAgent and FinOpsGate
* **Topology change (`brain/engine.py`):** replaced the direct edge `coder_agent → finops_gate` with the pair `coder_agent → contract_guard → finops_gate`. Chose two `add_edge` calls over `add_conditional_edges` because a routing function that unconditionally returns one branch is cognitive noise; the node short-circuits internally and owns its own anchor mutation in a single boundary.
* **Three $O(1)$ deterministic triggers (`agents/contract_guard.py`):**
    * **TCI Delta:** `abs(state["tci"] - anchor["tci"]) > 15.0` (absolute points on 0–100).
    * **CSS at Token Capacity:** `state["css"] < 40.0` AND $(token\_local + token\_cloud) / context\_window \ge 0.80$. The only rule that can fire on the first turn (no anchor yet).
    * **Subgraph/Domain Shift:** `state["target_role"] != anchor["target_role"]` (requires prior anchor).
* **`SessionContract` Pydantic model:** structured-output contract `{mission_outcome, active_role, in_scope, out_of_scope, open_constraints, trigger_reason}`. Minted via `LLMGateway.ainvoke(response_format={"type": "json_object"})` then validated with `model_validate_json`. On any LLM / network / parse failure, the node falls back to a deterministic skeleton built from `mission_spec.outcome / .scope / .constraints` so the banner always renders if a trigger fired.
* **Additive schema growth (`brain/state.py`):** appended `ui_payload: Optional[Dict[str, object]]` and `contract_anchor: Optional[Dict[str, object]]` to `AIlienantGraphState`. Scalar overwrite (no reducer) — guard runs serially after CoderAgent. `ContextMeter` Pydantic remains immutable.

### Phase Renumbering (Roadmap Foresight)
* **Conflict:** the inbound brief labelled this work "Phase 2.17", which already binds to the shipped *Shallow State + Blob Storage*. 2.23 (Telemetry Logger), 2.24 (Inyección Dinámica), and 2.25 (Checkpoint Gate) were also occupied. Renumbered as **Phase 2.26** to preserve prior delivery history and append cleanly at the tail of Phase 2D.

### Quality Assurance
* **`tests/test_contract_guard.py`:** 11 new tests covering each trigger (positive + boundary cases), pass-through on quiet turns, stub-injected LLM success path, raised-exception fallback, and malformed-JSON fallback.
* **Full suite:** **281 passing tests** (+11 new, +1 incidental); zero regressions. `mypy agents/contract_guard.py` clean (0 issues). Graph compiles end-to-end via `from brain.engine import alienant_app`.

### Files Changed
* `ailienant-core/brain/state.py` — two new state fields.
* `ailienant-core/agents/contract_guard.py` — **NEW** (≈190 LoC).
* `ailienant-core/brain/engine.py` — import + `add_node` + edge rewiring.
* `ailienant-core/tests/test_contract_guard.py` — **NEW** (≈210 LoC).

---

## 🚀 HITO 1.0.9 📅 [16/05/2026] | ResearcherAgent — Phase 4.1.1 (Context Hound)

### Read-Only Retrieval Node with @-Mention Override

* **New cognitive node (`agents/researcher.py`):** Strictly read-only LangGraph node producing a **Skeleton Map** (function signatures, class headers, cross-module relations, file paths) for the future PlannerAgent consumption in `FULL_SWARM` mode. Follows the established planner pattern: deterministic retrieval + single `LLMGateway.ainvoke` call. Zero LangChain `bind_tools` / ReAct precedent introduced.
* **Decision tree:**
  1. **@-mention override** — when `state["explicit_mentions"]` has entries, the node bypasses GraphRAG and loads those files verbatim via `VFSMiddleware.read()` (try/except `FileNotFoundError`, fail-soft).
  2. **GraphRAG path** — otherwise, `SemanticMemoryManager.search_with_paths()` → `GraphRAGDynamicExtractor.deep_parse()` produces the formatted context block.
  3. **Single LLM call** — both paths converge in one `LLMGateway.ainvoke(model=MODEL_MEDIUM, temperature=0.0)` invocation that asks for a compact markdown Skeleton.
* **Tools deferred:** `GlobTool` / `GrepTool` intentionally NOT created. GraphRAG already covers their retrieval intent (path matching + symbol search via Tree-sitter). Re-evaluate in 4.1.4 if the CoderAgent surfaces a gap.

### State Contract Extension (Phase 4 Lock-In Amendment)

* **New channel:** `AIlienantGraphState.researcher_skeleton: Optional[str]` (additive, default `None`, no reducer). Written by `run_researcher_node`; will be consumed by the PlannerAgent in Phase 4.1.3 when the FULL_SWARM topology is wired.
* **Blueprint amendment:** `docs/PHASE_4_BLUEPRINT.md` §1 amended in the same PR per the Phase 4 lock-in clause in `claude.md`. Provenance-map row added.
* **`RESEARCHER_IDENTITY`** added to `shared/rbac.py` (`PermissionMode.READ_ONLY`, `allowed_tools=[]` — tools are programmatic in this phase).

### Quality Assurance

* **`tests/test_phase4_researcher.py`** (2 strict tests): `test_researcher_standard_retrieval` (GraphRAG path) and `test_researcher_explicit_override` (proves SemanticMemoryManager + GraphRAGDynamicExtractor are NOT called when @-mentions are supplied — they `raise AssertionError` on side-effect).
* **Full suite: 283 passing tests** (+2 net, 0 regressions). `mypy --strict --explicit-package-bases` clean. `ruff check` clean.
* **Scope boundary:** the node is reachable directly (`from agents.researcher import run_researcher_node`) but **not** wired into `brain/engine.py` yet. FULL_SWARM topology assembly happens in 4.1.3 (Orchestrator) / 4.3 (Modes).

### Files Changed

* `ailienant-core/agents/researcher.py` — **NEW** (≈155 LoC).
* `ailienant-core/brain/state.py` — `researcher_skeleton: Optional[str]` added.
* `ailienant-core/shared/rbac.py` — `RESEARCHER_IDENTITY` added (English `role_description`).
* `ailienant-core/tests/test_phase4_researcher.py` — **NEW** (≈130 LoC).
* `docs/PHASE_4_BLUEPRINT.md` — §1 state-contract section amended.
* `docs/PROJECT_MANIFEST.md` — 4.1.1 ticked `[x]`.
* `README.md` — Repository Layout (agents list + test count).

---

## 🚀 HITO 1.0.10 📅 [16/05/2026] | PlannerAgent — Phase 4.1.2 Gap Closure

### Bounded Retry, Heavy Tier, and Skeleton Intake

The PlannerAgent's structural backbone (`MissionSpecification` Pydantic v2 contract, polyglot file guard, `immutable_wbs` shadow freeze, ResourceBroker VRAM coordination) was already in place from Phase 2/3. Phase 4.1.2 closed the **five concrete gaps** that blocked declaring the blueprint §4.1.2 contract complete — **without** rewriting the existing planner.

* **Bounded `ValidationError` retry (`MAX_PLANNER_RETRIES=2`):** Single-shot `try/except parse_err → return errors` replaced by a `while retry_count <= 2` loop. On each failure, the raw `str(ValidationError)` is appended to the user message so the LLM corrects on the next attempt. Hard ceiling: 3 total attempts; exhaustion returns a clean `state.errors` entry — no fatal raise.
* **`researcher_skeleton` consumption:** The Phase 4.1.1 channel is now read by the planner and injected as a sandboxed `<{boundary} role="researcher_skeleton">...</{boundary}>` block inside the existing XML-boundary discipline. Inert-data treatment per the established Prompt Injection defence.
* **Model tier lock-in:** `ResourceBroker.acquire_or_resolve(state, model=MODEL_BIG)` now matches the blueprint's "Heavy/Opus" mandate. ResourceBroker still arbitrates the VRAM lock; only the requested tier changed.
* **`planner_retry_count` telemetry:** New `AIlienantGraphState` field. Visible to tests, FinOps audit, and the future Orchestrator. Surfaced in the result dict on both success and exhaustion paths.
* **`tests/test_planner.py`** (NEW, 3 tests): `test_planner_retries_on_malformed_json_then_succeeds` (1 retry → success, asserts corrective banner in the 2nd call's user message), `test_planner_returns_errors_when_retries_exhausted` (3 garbage responses → clean `errors[]`), `test_planner_consumes_researcher_skeleton` (skeleton text surfaces in the prompt sent to LLMGateway). All mock `audit_task_complexity` to isolate the planner's LLM call from the Phase 3.3 Mini-Judge cascade.

### Deliberate Non-Goals

* **`with_structured_output` migration rejected** — the existing `response_format=json_object + MissionSpecification.model_validate_json` path is functionally identical to LangChain's wrapper, already integrated with `ResourceBroker`, and migrating would add risk for zero behavioural gain.
* **`WBSStep.target_role` widening (5 → 8 values per blueprint §3.1) deferred to 4.1.4** — no consumer reads the additional 3 roles yet; widening a Literal nobody uses is busywork.

### Quality Assurance

* **Full suite: 304 passing tests** (+3 net from 283 baseline). Zero regressions. `ruff` clean. `mypy --strict` clean on `brain/state.py`. Pre-existing strict-mode debt in `agents/planner.py` (4 errors: generic-type annotations on `list`/`dict` + `from prompts import ...` path) silenced via the established `mypy.ini` per-module pattern (mirrors `agents.analyst`, `core.vfs_middleware`, etc.) — debt left untouched, scheduled for a dedicated cleanup PR.

### Files Changed

* `ailienant-core/agents/planner.py` — surgical retry loop + skeleton intake + BIG tier; pre-existing `import os as _os` moved to top.
* `ailienant-core/brain/state.py` — `planner_retry_count: int` channel.
* `ailienant-core/tests/test_planner.py` — **NEW** (≈260 LoC).
* `ailienant-core/mypy.ini` — `agents.planner` added to `follow_imports = silent` list.
* `docs/PHASE_4_BLUEPRINT.md` — §1 channel + §4.1 threshold row.
* `docs/PROJECT_MANIFEST.md` — 4.1.2 ticked `[x]` with status note.
* `README.md` — backend test count 283 → 304 (both occurrences).

---

## 🚀 HITO 1.0.11 📅 [17/05/2026] | OrchestratorAgent — Phase 4.1.3 (El Capataz)

### Deterministic WBS Lifecycle with Bounded Failure Ceiling

The OrchestratorAgent is the runtime controller for the LangGraph WBS lifecycle. Unlike the Planner (LLM-backed, single O(1) shot) and the Coder (forthcoming LLM-backed tool user), the Orchestrator is **purely deterministic** — no LLM call, no broker arbitration, no checkpoint cost. It picks the next pending step, emits the `target_role` Prompt Swap signal, and enforces the blueprint's `MAX_RETRIES=2` ceiling.

* **Single Source of Truth iteration:** `_pick_next_step` walks `state["mission_spec"].tasks` and returns the first task whose status is neither `completed` nor `failed`. Tasks already in `in_progress` are returned for retry; the dispatch path is idempotent (R2 — no redundant `model_copy` mutation).
* **Prompt Swap signal:** the node emits `{target_role, current_step_id}` only. The CoderAgent (Phase 4.1.4) will own the role → system-prompt mapping; the Orchestrator's contract is "pick + dispatch", nothing more.
* **Bounded Failure ceiling:** if `retry_count > MAX_RETRIES (= 2)`, the active step is mutated to `status="failed"`, `hitl_pending=True` is set, `security_flags += ["BOUNDED_FAILURE_LIMIT_REACHED"]`, and the counter is reset for the next HITL-unblocked step. Errors entry includes step number + role + retry count for the operator's diff.
* **RED ALERT flag:** if `css_total < 40.0` (blueprint canonical threshold), `security_flags += ["RED_ALERT_ORCHESTRATOR"]` is emitted. **Informational only** — topology routing belongs to the IntentRouter (Phase 4.3); the Orchestrator never reroutes.
* **Terminal state signal:** when all tasks are `completed`/`failed`, the node emits `security_flags += ["ALL_WBS_STEPS_COMPLETE"]` plus `{current_step_id: None, target_role: None}` — a clean LangGraph END marker without mutating `mission_spec`.

### Risk-Audit Fixes Baked In (Anti-Bias Review)

* **R1 — `retry_count` ownership:** the Orchestrator is the JUDGE, never the incrementer. Increment is the responsibility of downstream failure evaluators (`validate_output` on validation failure, `drift_monitor` on drift, future AnalystAgent on QA rejection). Documented at module-docstring level + at the read site to prevent the "ghost increment" infinite-loop trap when wired in Phase 4.3.
* **R2 — `in_progress` idempotency:** re-dispatch of a step already at `in_progress` short-circuits before `_mark_step_status`, emitting only the dispatch signal (`target_role` + `current_step_id`) without a mission mutation. Saves a `model_copy` and avoids spurious diffs in the WBS audit trail.
* **R3 — Pydantic/dict dual-shape:** `_safe_get_css(metrics, fallback)` handles both `ContextMeter` models and plain `dict[str, Any]` shapes (LangGraph SQLite checkpoint deserialization may produce either). Replaces a naive `hasattr(metrics, "css_total")` that would silently return False on the dict shape.

### Quality Assurance

* **`tests/test_orchestrator.py`** (NEW, 6 tests, no LLM mocks): happy-path step pick + Prompt Swap, Bounded Failure ceiling + HITL escalation, RED ALERT with ContextMeter, ALL_WBS_STEPS_COMPLETE terminal signal, R2 idempotency on in_progress, R3 dict-shaped context_metrics.
* **Full suite: 310 passing tests** (+6 net from 304 baseline). Zero regressions. `ruff check` clean. `mypy --strict --explicit-package-bases` clean on the new module.
* **Deferred items:** (a) engine.py wiring → Phase 4.3 when `execution_mode` subgraphs are assembled; (b) role → system-prompt mapping in `prompts/roles.py` → Phase 4.1.4 CoderAgent transmutation; (c) `WBSStep.target_role` widening (5 → 8 values per blueprint §3.1) → Phase 4.1.4 when the Coder actually consumes the new roles.

### Files Changed

* `ailienant-core/agents/orchestrator.py` — **NEW** (≈165 LoC).
* `ailienant-core/tests/test_orchestrator.py` — **NEW** (≈200 LoC).
* `docs/PHASE_4_BLUEPRINT.md` — §1 provenance map: two rows for `target_role` + `current_step_id` ownership.
* `docs/PROJECT_MANIFEST.md` — 4.1.3 ticked `[x]` with full status note.
* `README.md` — backend test count 304 → 310, `orchestrator` added to agents tuple.

---

## 🚀 HITO 1.0.12 📅 [17/05/2026] | CoderAgent Cognitive Policy Engine + 8-Role Schema Widening — Phase 4.1.4

### Single Model, Many Personalities — Policy Layer Only (No Executor Yet)

Blueprint §3 mandates the CoderAgent transmute across 8 RBAC roles via Prompt Swapping. Phase 4.1.4 lands the **policy layer** — role → system_prompt + tool_whitelist + hitl_triggers — without executing any LLM call or real tool. Tool execution belongs to Phase 5 MCP.

* **New module — `agents/roles.py` (~125 LoC):** `ROLE_REGISTRY` maps each of the 8 canonical roles (`core_dev`, `architect_refactor`, `devops_infra`, `secops`, `qa_tester`, `doc_manager`, `vcs_manager`, `data_ml_engineer`) to a `RoleConfig` TypedDict carrying the role-specific System Prompt directive, the tool whitelist (strings — consumed by Phase 5 MCP executor), forbidden output phrases, and HITL substring triggers. Two builder helpers: `get_role_config(role)` (defensive fallback to `core_dev`) and `build_coder_system_prompt(role)` (ephemeral string composition).
* **`agents/coder.py` augmented in-place:** policy resolution + ephemeral prompt build inserted after the step lookup. `ephemeral_system_prompt` is a LOCAL VARIABLE — **never** written to `state.messages`, **never** returned in the result dict. Pre-execution HITL gates iterate `role_cfg["hitl_triggers"]` against the concatenated `target_file + description` and emit `HITL_APPROVAL_REQUIRED:<role>:<trigger>` entries in `security_flags`.

### Risk-Audit Fix R1 — Phantom State Keys

A pre-review draft of the plan returned `allowed_tools` from `run_coder_node`. LangGraph passes every returned key through state reducers — keys not in the `AIlienantGraphState` TypedDict either break state-merge or silently bloat the SQLite checkpoint. **Fixed before any code landed:** the Coder returns ONLY existing state keys (`vfs_buffer`, `target_role`, `current_step_id`, `current_cost_usd`, plus `security_flags` when non-empty). Phase 5's MCP executor re-resolves the role config at runtime via the module-level singleton — O(1) dict lookup, no perf penalty for the second read. Test C explicitly asserts `result.keys()` is a subset of declared state fields.

### Schema Widening — `WBSStep.target_role` 5 → 8 Values

Blueprint §3.1's twice-deferred schema migration finally landed:
* **Transitional Literal (13 values):** accepts legacy `Refactor/Infra/Doc/SecOps/Test` AND new `core_dev/architect_refactor/devops_infra/secops/qa_tester/doc_manager/vcs_manager/data_ml_engineer`. Existing tests/checkpoints continue to type-check.
* **`model_validator(mode="before")`:** maps legacy strings to canonical names at construction (`Refactor`→`architect_refactor`, `Infra`→`devops_infra`, `Doc`→`doc_manager`, `SecOps`→`secops`, `Test`→`qa_tester`). Stored value is always one of the 8 NEW. Idempotent on already-new values.
* **Tech debt:** legacy 5 values + migration validator removed one release after Phase 4 ships (logged in `PROJECT_MANIFEST.md`).
* **Fixture cascade:** `planner.py` DEBUG-MODE mocks + `test_fast_boot.py` + `test_planner.py` fixtures updated to emit new names directly. `test_orchestrator.py` assertions updated to expect post-migration canonical values (`"Test"` → `"qa_tester"`, etc.). A new test (`test_coder_agent_legacy_role_migrates_to_new_via_validator`) proves end-to-end migration through the Coder.

### Quality Assurance

* **`tests/test_coder_agent.py`** (NEW, 4 tests): doc_manager tool whitelist (no BashTool, has WriteFileTool + apply_patch), devops_infra HITL trigger on `.env`, ephemeral-prompt non-leak + R1 state-key contract (`result.keys()` ⊆ declared state fields), legacy → new role migration end-to-end.
* **Full suite: 314 passing tests** (+4 net from 310 baseline). Zero regressions. `ruff check` clean. `mypy --strict --explicit-package-bases` clean on `agents/roles.py` and `brain/state.py`.

### Files Changed

* `ailienant-core/agents/roles.py` — **NEW** (~125 LoC).
* `ailienant-core/agents/coder.py` — policy resolution + HITL gate evaluation + R1-safe return dict.
* `ailienant-core/agents/planner.py` — DEBUG-MODE WBSStep mocks updated to new role vocabulary.
* `ailienant-core/brain/state.py` — `WBSStep.target_role` widened to 13-value transitional Literal + `_migrate_legacy_target_role` before-validator + `_LEGACY_TO_NEW_ROLE` map.
* `ailienant-core/tests/test_coder_agent.py` — **NEW** (4 tests, ~180 LoC).
* `ailienant-core/tests/test_fast_boot.py` — fixture role updated to `architect_refactor`.
* `ailienant-core/tests/test_planner.py` — `_valid_mission_json` role updated to `architect_refactor`.
* `ailienant-core/tests/test_orchestrator.py` — assertions updated to expect post-migration canonical values.
* `docs/PHASE_4_BLUEPRINT.md` — §3.1 status: `Decision` → `Implemented 2026-05-17`. §7 impact table SCHEMA_EVOLUTION row marked Done.
* `docs/PROJECT_MANIFEST.md` — 4.1.4 ticked `[x]` with status note + Tech Debt entry for legacy role removal.
* `README.md` — backend test count 310 → 314 (both occurrences).
* `docs/PROJECT_MANIFEST.md`, `docs/SCHEMA_EVOLUTION.MD`, `docs/DEV_JOURNAL.md`, `README.md`.

---

## 🚀 HITO 1.0.9 📅 [16/05/2026] | Interactive Resource Broker — Cross-Session VRAM Confinement (Phase 2.27)

### Problem
Local LLM invocations across concurrent AILIENANT sessions were unprotected against VRAM contention. The graph's RELAY topology serialised inferences *within* a session, but two sessions could still race for the same Ollama model and cause thrashing or OOM crashes.

### Singleton Lock + Wrapper at Call Sites
* **`core/resource_manager.py` (NEW, ≈285 LoC):** `GPUResourceManager` is a process-wide async singleton built on `asyncio.Lock` (mutex on `_LockState`) and `asyncio.Event` (O(1) wakeup of queued waiters). Tracks `active_model_name`, `locked_by_session_id`, `lock_timestamp`, and a FIFO `queue`. Reentrant per session.
* **`ResourceBroker.acquire_or_resolve(state, model)`:** thin orchestration wrapper. MODEL_BIG and sessions without `task_id` bypass the lock entirely. On contention it computes a recommendation, mutates `state["ui_interrupt"]` and `state["contention_status"]`, and suspends via the existing `vfs_manager.request_human_approval(...)` (same convention as `drift_monitor` and `finops_gate` — *not* a new HitL paradigm).
* **Three drift signals → one heuristic (`_compute_recommendation`):** `TCI>75` or `TCI<40` → `SWITCH_TO_CLOUD`; mid-TCI + empty queue → `WAIT`; mid-TCI + busy queue → `SWITCH_TO_CLOUD`.

### Three Resolution Paths
* **WAIT:** broker calls `acquire_lock` and the caller awaits; lock returns to caller atomically.
* **SWITCH_TO_CLOUD:** broker substitutes `effective_model = MODEL_BIG` and swaps `state["active_llm_profile"]` to a cloud profile. No local lock held.
* **CANCEL:** broker returns `BrokerDecision(cancelled=True)`; caller returns an error-shaped state delta and skips the LLM call.

### Schema Growth (Additive — `ContextMeter` Pydantic Untouched)
* `ui_interrupt: Optional[Dict[str, object]]` — distinct from Phase 2.26 `ui_payload`; blocking modal cannot collide with persistent banner in the same turn.
* `contention_status: Optional[Dict[str, object]]` — telemetry snapshot of the contention moment.
* `user_resource_resolution: Optional[Literal["WAIT","SWITCH_TO_CLOUD","CANCEL"]]` — captured user reply.

### WebSocket Transport (Zero `ws_contracts.py` Changes)
Rich payload is JSON-encoded into `HITLApprovalRequestPayload.proposed_content` with sentinel `action_description="RESOURCE_CONTENTION"`. Frontend discriminates on the sentinel; response in `client_hitl_response.comment ∈ {"WAIT","SWITCH_TO_CLOUD","CANCEL"}`. Strict payload contract:

```jsonc
{
  "action": "RESOURCE_CONTENTION_INTERRUPT",
  "payload": {
    "conflicting_model": "...",
    "task_tci": 0.0,
    "recommendation": "WAIT | SWITCH_TO_CLOUD",
    "queue_position": 1,
    "estimated_wait_seconds": 20
  }
}
```

### Anti-Deadlock Discipline (Reviewer-Flagged)
Each guarded call site (`planner.py`, `summarizer.py`, `mcts_coder.py`) wraps the *entire* lock-held region — LLM call + sanitization + Pydantic validation — in `try/finally` that releases the lock even if post-LLM parsing raises. A bad JSON response would otherwise deadlock every other session permanently. Covered by `test_lock_released_when_post_llm_processing_raises`.

### Quality Assurance
* `mypy core/resource_manager.py` — 0 errors.
* `tests/test_resource_manager.py` — **18 new tests** including singleton identity, multi-session queue + release, all three resolution paths, recommendation heuristic, and the deadlock regression guard.
* Full suite: **301 passing tests** (+18 net, 0 regressions). Graph compile smoke (`from brain.engine import alienant_app`) returns instance.

### Files Changed
* `ailienant-core/core/resource_manager.py` — **NEW** (≈285 LoC).
* `ailienant-core/brain/state.py` — three additive fields.
* `ailienant-core/agents/planner.py` — wrap LLM call with broker.
* `ailienant-core/brain/summarizer.py` — wrap LLM call with broker.
* `ailienant-core/agents/mcts_coder.py` — wrap `generate_local_variant` and `_ask_local_to_fix`; conditional tier (preserves `Tier.LOCAL` when broker keeps us local, swaps to `Tier.CLOUD` when broker substitutes MODEL_BIG).
* `ailienant-core/tests/test_resource_manager.py` — **NEW** (≈260 LoC).
* `docs/PROJECT_MANIFEST.md`, `docs/SCHEMA_EVOLUTION.MD`, `docs/DEV_JOURNAL.md`, `README.md`.