# AILIENANT 🐜: Hybrid Agentic Orchestrator for Software Engineering

## 1. Visión General
AIlienant es una plataforma de ingeniería asistida por IA que opera como una extensión nativa de VS Code, ofreciendo un entorno de ejecución agentic similar a Claude Code. Diseñado para maximizar la eficiencia costo-beneficio, utiliza un enrutador híbrido de LLMs basado en complejidad, una memoria de proyecto evolutiva y una interfaz de telemetría centrada en el control de tokens.

El sistema evoluciona hacia una arquitectura de estados dirigida por **LangGraph**, permitiendo flujos iterativos y corrección de errores en tiempo real. Se integra una memoria **GraphRAG** escalable que prioriza archivos de texto plano y Markdown, optimizando la precisión mediante el análisis de dependencias estructurales. Además, implementa un enrutamiento inteligente de modelos (Small, Medium, Big, Cloud) para maximizar la velocidad de respuesta y la eficiencia de costos según la complejidad de la tarea. Para garantizar que el sistema opere con "Latencia Cero" en entornos de hardware heterogéneo (On-Premise) sin colapsar la máquina del usuario, la arquitectura implementa una doble estrategia de protección de VRAM. Primero, ejecuta un **Environment Profiling (Introspección de Hardware)** con un costo de $O(1)$ al inicio de la sesión, mapeando los límites matemáticos de los modelos disponibles (Parámetros, Cuantización y Ventana de Contexto) para prevenir crashes por *Out-of-Memory* (OOM). Segundo, la topología de LangGraph aplica **Context Pruning (Poda de Contexto)**. En lugar de transferir todo el estado (`AgentMemorySnapshot`) en cada salto de nodo, el Orquestador segmenta la información, garantizando que los agentes especializados reciban únicamente la fracción de código $O(N)$ estrictamente necesaria para su labor, eludiendo así el devastador costo cuadrático de atención $O(N^2)$ en los Transformers subyacentes.

AIlienant incluye un **Control Center** basado en Webview (React) que permite la supervisión en tiempo real de KPIs (uso de tokens, latencia, CSS score) y una interfaz visual interactiva para la gestión de la memoria GraphRAG, permitiendo auditar y editar las conexiones semánticas directamente desde la IDE. El sistema incluye a "La Hormiga" (`AnalystAgent`), un agente supervisor y chatbot socrático que actúa como analista, instructor y control de calidad de la arquitectura.

---

## 2. Características Principales

* **Orquestación Dirigida por Estados (LangGraph):** Capacidad para ejecutar ciclos condicionales, revisar código, ejecutar tests y auto-corregirse sin perder el contexto de la tarea.
* **Memoria GraphRAG Especializada:** Combina búsqueda vectorial (LanceDB) con un mapeo de relaciones topológicas de código (NetworkX), enfocado exclusivamente en archivos de texto plano y `.md`.
* **Smart LLM Routing (Local & Cloud):** Selección dinámica entre modelos *Small* (ej. 1.5B para tareas simples), *Medium* (8B), *Big* (32B Coder) o *Cloud* (Claude 3.5 / GPT-4o) basada en el Context Sufficiency Score (CSS).
* **Motor de Inferencia Universal (Bring Your Own Model):** AILIENANT está diseñado bajo un paradigma estrictamente **agnóstico de hardware y de proveedor** compatible con el estandar OpenAI API. No te atamos a un ecosistema cerrado. A través de nuestro **LLM Router Híbrido** y la abstracción de LangChain, la plataforma es capaz de ingerir y orquestar cualquier modelo de lenguaje, garantizando el equilibrio perfecto entre velocidad, costo y privacidad.

El sistema soporta la inyección de cualquier modelo que cumpla con los estándares de generación de texto, ya sea comercial, *open-weights*, *distilled* (destilado) o con *fine-tuning* personalizado para tu stack tecnológico.

### ⚡ Topologías de Ejecución Soportadas

* **☁️ Cloud-Native (Máxima Capacidad de Razonamiento):**
    A través del archivo `.env`, puedes inyectar API Keys para modelos fundacionales pesados (ej. OpenAI GPT-4o, Anthropic Claude 3.5, Google Gemini). El router derivará a estos cerebros únicamente las tareas de alta entropía (arquitectura, refactorización profunda).
* **🖥️ Local & Edge (Máxima Privacidad y Latencia Cero):**
    Integración directa vía `langchain-community` con motores locales como **Ollama, vLLM o Llama.cpp**. Esto permite que la IA corra directamente en el silicio de tu máquina sin que el código fuente salga de tu red local. Ideal para modelos destilados veloces (ej. *Llama-3-8B-Instruct*, *Qwen-Coder* o modelos *fine-tuned* empresariales).
* **🔀 Smart Routing & Fallback (Eficiencia Dinámica):**
    AILIENANT no usa un solo modelo para todo. El grafo de agentes inyecta las peticiones simples (ej. documentar una función) al modelo local más rápido y económico, mientras reserva los modelos pesados para el *Orchestrator* o el *LogicAgent*. Si el hardware local (VRAM) se satura o colapsa, el sistema aplica una **Degradación Elegante**, enrutando temporalmente al Cloud para evitar bloqueos en el IDE.

### ⚙️ Configuración (Plug & Play)

No se requieren cambios en el código del núcleo. La inteligencia se conecta configurando variables de entorno o apuntando al puerto de tu motor local (`localhost:11434` para Ollama, por defecto). El grafo de LangGraph abstrae automáticamente los formatos de entrada y salida, asegurando que los agentes sigan funcionando sin importar qué cerebro esté conectado "bajo el capó".
* **Environment Awareness:** El sistema lee el contexto de VS Code (extensiones, linters, formato, OS) para generar código que respete la configuración y estilo del entorno local del desarrollador.
* **Integración MCP (Model Context Protocol):** Soporte nativo para integrar servidores MCP y *skills* personalizadas. El usuario puede conectar bases de datos, APIs externas o herramientas del sistema operativo mediante un estándar universal, expandiendo las capacidades del agente a demanda.

---

## 2.5. 🧠 Arquitectura Central y Resiliencia (Local MVP)

Para garantizar que Ialienant opere como un motor autónomo 100% local sin colapsar el hardware del desarrollador ni asfixiar el IDE, hemos implementado una arquitectura de ultra-eficiencia con las siguientes protecciones estructurales:

### 1. Gestión de Memoria y Contexto (Prevención de OOM)
* **Tiered Caching (Perfilado de Hardware):** El sistema incluye un escáner de recursos que evalúa la RAM/VRAM en tiempo real. Mantiene modelos de enrutamiento rápidos (ej. 1.5B) en la memoria unificada/RAM para latencia cero, y carga modelos de razonamiento lógico solo cuando los recursos lo permiten.
* **State Summarization (Ventana Deslizante):** Para evitar el desbordamiento de tokens en sesiones largas de *debugging*, un nodo interceptor en LangGraph monitoriza el límite de contexto. Al acercarse al 80%, comprime el historial antiguo en un resumen denso usando el modelo ligero, manteniendo intactos los últimos 3 a 5 turnos para preservar la inmediatez cognitiva.
* **Tiered Checkpointing:** El historial a corto plazo (L1) vive en la RAM para permitir *Time-Travel Debugging* instantáneo y retrocesos (Ctrl+Z) sin latencia, mientras que la persistencia a largo plazo (L2) se escribe en el disco en lotes.

### 2. Concurrencia y Protección de I/O (Anti-Freeze)
* **Aislamiento en ProcessPoolExecutor:** Toda la carga pesada dependiente del CPU (parseo de AST, cálculo de matrices en grafos) se aísla en procesos hijos. El Event Loop asíncrono principal (FastAPI) nunca se bloquea, garantizando que los WebSockets y la UI del editor respondan en tiempo real.
* **Debouncing y Event Coalescing:** Para sobrevivir a guardados masivos o formateadores automáticos (ej. *Prettier* modificando 20 archivos de golpe), los eventos se agrupan en una "sala de espera" temporal (ej. 500ms) y se envían como un único lote al proceso hijo, protegiendo la base de datos `SQLite WAL` de bloqueos por escritura concurrente.
* **Dynamic Thresholding (Umbrales de Estrés):** El sistema evalúa el "radio de explosión" de los eventos. Cambios menores van por la ruta rápida; eventos masivos (más de 100 archivos modificados de golpe) activan automáticamente una ruta de emergencia con prioridad baja para no bloquear el Sistema Operativo.

### 3. Motor GraphRAG y Sincronización Diferencial
* **PPR + Skeleton-Flesh Prompting:** En lugar de inyectar archivos completos a la fuerza bruta, el sistema inyecta el código completo (*Flesh*) solo del archivo donde el usuario está trabajando. Para dependencias lejanas, utiliza el AST para extraer solo las firmas y estructuras (*Skeleton*), guiado por algoritmos de *Personalized PageRank*. Esto erradica el ruido semántico y ahorra miles de tokens.
* **Lazy Indexing (Mitigación de Cold Start):** Al abrir un monorepo inmenso por primera vez, la indexación se realiza asíncronamente en segundo plano. El sistema opera en "Modo de Contexto Parcial" hasta terminar, gestionando la expectativa en la UI sin congelar el IDE.
* **Sincronización Diferencial de Arranque (Boot Diff Check):** Al reabrir VS Code (Día 2 en adelante), Ialienant no re-indexa todo. Compara los timestamps (`last_modified`) del Sistema Operativo con la base de datos en milisegundos, indexando quirúrgicamente solo los archivos que hayan cambiado mientras el IDE estuvo cerrado.
* **Graph Pruning (Consciencia de Git):** Ante cambios masivos de rama (`git checkout`), el sistema procesa obligatoriamente las eliminaciones primero. Purga los nodos huérfanos de SQLite y los vectores de LanceDB antes de actualizar el resto, erradicando el riesgo de "alucinaciones por dependencias fantasma".

---

## 3. Innovaciones en Gestión de Contexto y Memoria 🧠

Para resolver la "amnesia de proyecto" y la saturación de tokens, Ailienant implementa tres pilares de innovación arquitectónica:

### 3.1. Gestión Jerárquica de Memoria (Global vs. Local)
A diferencia de los agentes estándar, utilizamos un sistema de configuración en cascada inspirado en sistemas operativos:
* **Capa Global:** Credenciales de API y preferencias de usuario (almacenadas de forma segura en el OS).
* **Capa de Proyecto (`.ailienant/config.json`):** Reglas específicas del repositorio.
* **Impacto:** Permite **Personalización por Entorno**. El "Proyecto A" puede forzar el uso de modelos locales (Llama-3) por privacidad, mientras que el "Proyecto B" puede utilizar modelos Cloud (Claude 3.5 Sonnet) para tareas de alta complejidad.

### 3.2. Cognitive Persistence Files (`AGENTS.md`)
Implementamos una memoria de largo plazo de bajo costo mediante archivos de persistencia cognitiva.
* **Mecánica:** El agente mantiene un archivo `AGENTS.md` que actúa como un "resumen ejecutivo" del estado del sistema, decisiones de diseño y deudas técnicas.
* **Eficiencia:** Reduce la latencia de recuperación de context de $O(n)$ a $O(1)$, eliminando la necesidad de re-indexar el GraphRAG completo en cada consulta y optimizando drásticamente el consumo de tokens en el primer turno.

### 3.3. Filtro de Relevancia Git-Aware
El `ContextBuilder` de Ailienant no es estático; es consciente del estado del control de versiones.
* **Ponderación Dinámica:** Utilizamos `git status` y `git diff` para calcular la prioridad de los archivos. Los archivos en `staged` o recientemente modificados reciben un peso mayor en el cálculo del **Context Sufficiency Score (CSS)**.
* **Impacto:** Si preguntas por un error, el sistema prioriza los archivos que *estás cambiando actualmente*, garantizando que la solución sea relevante al código "caliente" y no a partes obsoletas del repositorio.

---

## 4. Arquitectura Interna y Patrones Avanzados

Para garantizar resiliencia, seguridad y escalabilidad empresarial, Ialienant incorpora patrones de diseño avanzados inspirados en herramientas de orquestación de vanguardia (como Claude Code), adaptados a nuestro ecosistema Python/LangGraph.

### 🧩 4.1. Adaptador Transparente MCP (Adapter Pattern)
Ialienant no consume herramientas externas de forma directa y acoplada. Implementamos un patrón `Adapter` mediante nuestra clase `McpToolAdapter`, que envuelve cualquier servidor Model Context Protocol (MCP) remoto y lo expone como una herramienta nativa (`BaseTool`) de LangChain.

* **Aislamiento de Lógica:** Los agentes (ej. `LogicAgent`) interactúan con una interfaz estándar. Ignoran si la herramienta es un script local de Python o un servidor Node.js remoto ejecutando en un contenedor.
* **Ejecución Asíncrona Pura:** El adaptador traduce las llamadas síncronas del LLM en peticiones asíncronas (`_arun`) a través de la capa de transporte MCP, evitando bloqueos en el Event Loop de FastAPI.

### 📈 4.2. Evolución del Estado del Grafo (State Management)
El motor de LangGraph opera sobre un esquema de datos estrictamente tipado (`IalienantGraphState`). Hemos evolucionado este estado para que no solo transporte el contexto del código y los mensajes, sino también telemetría crítica de la sesión:

* **Inyección de Telemetría:** El estado ahora porta un sub-modelo `TelemetryData` que rastrea el `total_tokens_used`, `current_cost_usd` y métricas de latencia.
* **Inmutabilidad Controlada:** Agentes específicos tienen permisos de escritura delegados sobre claves específicas del estado (ej. el `AnalystAgent` opera en modo *Read-Only* sobre el código, mientras que el `SecOpsAgent` puede mutar los `security_flags`).

### 💰 4.3. FinOps Activo y Presupuestos Hard-Limit
A diferencia de los orquestadores estándar que pueden incurrir en bucles infinitos y costos descontrolados, Ialienant implementa un sistema de protección financiera a nivel de nodo:

* **Cost Tracking en Tiempo Real:** Cada transición de nodo en LangGraph actualiza el `current_cost_usd` basado en el modelo dinámico enrutado (Small, Medium, Big).
* **Interrupción HITL (Human-In-The-Loop):** Si el costo acumulado de una tarea alcanza el `max_budget_usd` definido en la configuración, el grafo lanza una excepción controlada (Hard-Stop). Esto suspende la ejecución, preserva el estado actual en memoria y envía un evento al WebSocket de VS Code para solicitar autorización explícita del usuario antes de continuar.

### 🛠️ 4.4. Dynamic Skill Registry y RBAC para Herramientas (Skills Management)
Ialienant abandona el paradigma rígido de "herramientas inyectadas en prompts". Implementa un motor de `SkillRegistry` basado en RBAC (Role-Based Access Control) inspirado en arquitecturas de agentes enjambre (Team Swarm).

* **Desacoplamiento de Prompts:** Los agentes no conocen qué herramientas existen hasta el momento de su ejecución. El motor inyecta las `BaseTools` (Nativas o MCPs) en el modelo dinámicamente (`llm.bind_tools()`) basándose en los permisos de su Rol (ej. el `InfraAgent` solo ve herramientas de Docker/AWS).
* **Filtros de Seguridad a Nivel Usuario:** El desarrollador puede pasar configuraciones (whitelist/blacklist de herramientas) por sesión desde VS Code, revocando instantáneamente el acceso a herramientas peligrosas (ej. ejecución de terminal) sin modificar el código base.
* **Plug & Play Extensibility:** Las nuevas habilidades (servidores MCP externos o scripts de Python) se autodescubren y se registran en el pool global en tiempo de ejecución, permitiendo que la inteligencia del sistema escale modularmente.

### 🐝 4.5. Paralelización mediante Team Swarms
Los agentes estratégicos (como el `PlannerAgent`) cuentan con la capacidad (Skill) nativa de hacer `Spawn` de sub-agentes en tiempo de ejecución. Utilizando la API asíncrona de sub-grafos de LangGraph, Ialienant puede desplegar múltiples instancias del `LogicAgent` simultáneamente para abordar refactorizaciones multi-archivo, reduciendo drásticamente la latencia total de la tarea.

---

## 5. Arquitectura de Mitigación de Riesgos y Control de Latencia 🛡️

Para garantizar que el motor híbrido no colapse el rendimiento del IDE ni sufra degradación, AILIENANT implementa protocolos estrictos de contingencia:

### 5.1. Protecciones de Rendimiento y UX
* **Graceful Timeout del TTFT:** Si la caminata por el grafo (GraphRAG) excede un límite estricto (ej. 800ms), el sistema aborta el recorrido y hace un *fallback* a RAG Semántico puro. Esto garantiza que el streaming de WebSockets hacia la UI nunca se congele.
* **Virtualización UI:** React Flow utiliza técnicas de Nivel de Detalle (LOD) para renderizar únicamente clústeres de memoria adyacentes a la tarea activa, protegiendo la RAM de VS Code.

### 5.2. Integridad de Datos y Ciclo de Vida (Memory Lifecycle)
* **Pipeline de Indexación Transaccional (SSoT):** Para evitar la desincronización ("Split-Brain") entre la base vectorial y el grafo en memoria, las actualizaciones se procesan mediante un pipeline transaccional. Si una mutación falla en NetworkX, se revierte en LanceDB.
* **Garbage Collection Vectorial:** Mediante análisis de `git diff`, el sistema aplica *Tombstones* (lápidas) a los vectores de código eliminado, previniendo alucinaciones basadas en contexto obsoleto.

### 5.3. Eficiencia de Costos (FinOps) y Prevención de Bloqueos
* **Indexación Perezosa (Lazy Cold-Start):** En proyectos masivos, AILIENANT escanea inicialmente solo el "esqueleto" del proyecto (nombres y firmas). La vectorización profunda ocurre de manera *Lazy*, activada únicamente cuando el usuario abre un archivo o un agente lo solicita, evitando bloqueos de CPU y límites de Rate (HTTP 429).
* **Inyección por Esqueletos de Contexto (AST-Chunking):** En lugar de inyectar archivos completos de miles de líneas en el prompt, el `ContextBuilder` inyecta un "mapa" estructural del archivo junto con el bloque de código exacto modificado, reduciendo drásticamente el Token Drain sin sacrificar comprensión global.

---

## 6. Resiliencia, Seguridad y Observabilidad (Enterprise Grade)

Para alcanzar los estándares de producción de la industria y garantizar que Ialienant pueda operar de forma autónoma sin comprometer la máquina del desarrollador, la arquitectura implementa tres pilares de resiliencia avanzada:

### 🛡️ 6.1. Aislamiento de Ejecución Estricto (Sandboxing)
Dar autonomía a una IA para ejecutar código, tests o comandos de terminal conlleva riesgos inherentes (ej. borrado accidental de archivos o consumo de recursos). Ialienant mitiga esto abandonando la ejecución local directa.


* **Wasm/gVisor Sandboxing:** El `TestAgent` y el `InfraAgent` ejecutan sus rutinas de validación dentro de entornos aislados basados en WebAssembly (Wasm) o contenedores protegidos por gVisor. 
* **Zero-Host-Damage:** El código generado y ejecutado por la IA interactúa con un sistema de archivos virtual y efímero. Incluso en caso de una alucinación severa del modelo (ej. ejecutar `rm -rf /`), la máquina host del usuario permanece criptográficamente y físicamente segura.

### 🧠 6.2. Metacognición y Prevención de Cuellos de Botella (Anti-SPOF)
En arquitecturas de agentes tradicionales, el Orquestador es un Punto Único de Fallo (SPOF). Si toma una mala decisión, el sistema entra en un bucle infinito. Ialienant resuelve esto implementando un **Monitor de Salud del Grafo (Graph Health Monitor)**.


* **Nodo Supervisor Independiente:** Un nodo metacognitivo audita las transiciones de estado de LangGraph de forma paralela. 
* **Circuit Breaker Cognitivo:** Si el supervisor detecta anomalías en el flujo (ej. el `LogicAgent` y el `TestAgent` rebotan entre sí más de 3 veces sin resolver un error), actúa como un *Circuit Breaker*. Interrumpe el flujo automático, congela el estado y escala el problema al usuario (HITL) o al `AnalystAgent` para un diagnóstico socrático.

### 🔍 6.3. Observabilidad Profunda (Graph Tracing)
Depurar sistemas no deterministas (como un grafo de agentes LLM) requiere herramientas de telemetría de vanguardia.


* **Integración Nativa con LangSmith:** Ialienant exporta trazas detalladas de cada ejecución. Los desarrolladores pueden visualizar el árbol de decisiones exacto, los tokens consumidos por nodo, la latencia de las llamadas a LanceDB y el payload exacto de cada uso de herramienta (MCP).
* **Time-Travel Debugging:** Gracias a la persistencia del `IalienantGraphState`, el sistema permite retroceder en el tiempo a un nodo específico del grafo para entender por qué el `OrchestratorAgent` tomó una decisión de enrutamiento particular, facilitando el ajuste fino de los prompts y la lógica del negocio.

---
## 7. Stack Tecnológico y Construcción (Arquitectura Híbrida)

El sistema utiliza un SSoT (Single Source of Truth) basado en IalienantGraphState, una estructura de datos estricta que garantiza la coherencia entre el orquestador LangGraph y la interfaz de VS Code, permitiendo telemetría en tiempo real y persistencia de estados mediante Checkpointing local.

Para construir Ialienant con una base sólida y escalable, el sistema se divide en un cliente nativo para el IDE y un motor de orquestación en Python:

* **Frontend (Extensión VS Code):** TypeScript para la integración con la API del IDE, y **React + Vite** para el Webview Dashboard (Panel de control, visualización del grafo de memoria y métricas).
* **Core Backend / Orquestador:** FastAPI + **LangGraph** (manejo de estados iterativos y grafos de flujo) + Pydantic (validación estricta).
* **Modelos Locales:** Soporte optimizado para vLLM, Llama.cpp, Ollama..."
* **Protocolo de Comunicación:** Inferencia vía OpenAI SDK / REST para garantizar compatibilidad con cualquier servidor de LLMs local o remoto.
* **Motores LLM (Smart Routing):**
    * *Local Small (ej. Qwen 2.5 1.5B):* Tareas de bajo esfuerzo computacional (formateo, docstrings).
    * *Local Medium/Big (ej. Llama 3 8B / Qwen Coder 32B):* Lógica de negocio y refactorización profunda.
    * *Cloud (ej. Claude 3.5 Sonnet):* Alta entropía, razonamiento complejo o falta de contexto local.
* **Agent Memory (GraphRAG Exclusivo Text/.md):**
    * *Vectorial:* LanceDB (búsqueda semántica ultrarrápida, 100% local).
    * *Topológica:* NetworkX en Python para mapear dependencias y relaciones estructuradas en archivos `.md`.
* **Environment Awareness:** Módulo lector de configuración de VS Code (extensiones, linters, tabulación, OS) para alinear el código generado con el perfil del desarrollador.
* **Estructura del Repositorio:** El proyecto sigue un modelo de monorepositorio organizado en:

    * */ailienant-core:* Servidor de orquestación (Python, FastAPI, LangGraph).
    * */ailienant-extension:* Interfaz de usuario y plugin de IDE (TypeScript, React).
    * */docs:* Documentación técnica, contratos de API y esquemas de evolución de estados.

**Estructura del Monorepositorio:**
El proyecto sigue un diseño modular para separar el entorno de ejecución del cliente:
* `/ailienant-core`: Servidor de orquestación y motor de agentes (Python).
* `/ailienant-extension`: Interfaz de usuario y plugin de IDE (TypeScript, React).
* `/docs`: Documentación técnica, contratos de API (`API_CONTRACTS.json.md`) y esquemas de estado (`SCHEMA_EVOLUTION.MD`).

**Dependencias Core (Backend):**
* `FastAPI` + `Uvicorn`: Servidor ASGI de alto rendimiento para WebSockets.
* `LangGraph`: Orquestación cíclica y manejo del estado (`AilienantGraphState`).
* `Pydantic`: Validación estricta de sub-estados y tipado en tiempo de ejecución.
* `LanceDB` + `NetworkX`: Motores de memoria vectorial y topológica (GraphRAG).
* `MCP`: Model Context Protocol para el uso de herramientas externas.

**Dependencias de la Extensión (Frontend):**
* `TypeScript`: Tipado estricto para la lógica de la extensión.
* `ws`: Cliente de WebSockets para comunicación bidireccional y telemetría en tiempo real.
* `VS Code API`: Interfaz nativa para manipulación del IDE.

---

## 8. Guía de Inicio Rápido (Desarrollo)

### 🚀 Ejecución del Motor Core (Backend)
El sistema utiliza un **SSoT (Single Source of Truth)** basado en `IalienantGraphState`. Para levantar el motor de orquestación con recarga automática:

1. Acceder al directorio: `cd alienant-core`
2. Activar entorno virtual: `.\venv\Scripts\activate` (Windows) o `source venv/bin/activate` (Unix).
3. Ejecutar el servidor: `uvicorn main:app --reload`

El servidor estará disponible y escuchando conexiones WebSocket en: `ws://localhost:8000/ws/v1/stream/{task_id}`
---

## 9. Estructura de la Memoria (AgentMemory)

## Definición del Componente
La Memoria del Agente (AgentMemory) es el motor GraphRAG del sistema. Transformando el código y documentación en un Grafo de Conocimiento (NetworkX) y vectores (LanceDB), actúa como la "Persistencia Cognitiva" optimizada exclusivamente para texto plano y archivos `.md`.

El componente de memoria es el motor de contexto del sistema. Se compone de los siguientes módulos operativos:

* **`semantic_upsert`**: Interfaz directa con LanceDB. Convierte texto de código en embeddings y los inserta o actualiza, manejando la duplicidad de datos mediante hashing.
* **`dependency_mapper`**: Utiliza análisis estático (AST) para extraer importaciones y llamadas a funciones. Construye el grafo de dependencias y escribe/actualiza archivos `.md` con sintaxis de enlaces bidireccionales `[[archivo]]`.
* **`context_compressor`**: Toma los resultados de una búsqueda GraphRAG y elimina redundancias, dejando solo los fragmentos de código y metadatos estrictamente necesarios.
* **`drift_analyzer`**: Compara el hash del archivo actual en disco contra el hash almacenado. Si hay discrepancia, activa un re-indexado selectivo.
* **`mcp_gateway`**: Gestiona las conexiones con los servidores del Model Context Protocol, exponiendo las herramientas externas al orquestador de manera segura.

---

## 10. Interacción con "La Hormiga" (AnalystAgent)

El **AgentMemory** suministra la materia prima, pero es el **AnalystAgent** quien la interpreta. 
* Cuando el usuario pregunta: *"¿Cómo funciona este proyecto?"*, el AgentMemory usa `dependency_mapper` para entregarle a la Hormiga el mapa topológico del código, permitiendo respuestas arquitectónicas precisas, impulsadas por el contexto del IDE y potenciadas por las herramientas del MCP conectado.

---

## 11. Arquitectura de Agentes: Skills & MCPs

El sistema utiliza el **Model Context Protocol (MCP)** para estandarizar cómo los agentes interactúan con el entorno local, las APIs y las herramientas del usuario. El flujo de trabajo está diseñado como una cadena de montaje dentro de LangGraph.

Para garantizar la estabilidad del grafo de ejecución, cada agente opera bajo un contrato estricto de Entrada/Salida (I/O Contract) y posee acceso limitado únicamente a las herramientas MCP pertinentes a su dominio, reduciendo vectores de ataque por alucinación y minimizando el gasto de tokens.

### 🧭 PlannerAgent (El Estratega)
* **Skills:** Work Breakdown Structure (WBS), diseño de sistemas, resolución de dependencias lógicas, estimación de complejidad.
* **MCPs / Tools:**
    * `project_board_sync`: Crea y lee tareas en el panel del proyecto (ej. GitHub Projects local) para mantener registro del plan.
    * `step_checkpoint_manager`: Gestiona el estado de LangGraph marcando los pasos del plan de ejecución como "completados", "fallidos" o "en progreso".

### 🧠 OrchestratorAgent (El Cerebro)
* **Skills:** Evaluación de entropía, cálculo del Context Sufficiency Score (CSS), delegación de estados y ejecución del plan creado por el PlannerAgent.
* **MCPs / Tools:** * `smart_llm_router`: Decide si usar Small, Medium, Big o Cloud.
    * `env_profile_reader`: Inyecta el contexto de extensiones y preferencias del usuario de VS Code.

### 🐜 AnalystAgent (La Hormiga / Crítico & Instructor)
* **Skills:** Onboarding socrático, auditoría de arquitectura, revisión de calidad (QA) y explicación de código.
* **MCPs / Tools:**
    * `graph_reader`: Navega por las relaciones de NetworkX para entender el impacto topológico.
    * `webview_communicator`: Envía alertas y métricas en tiempo real al Dashboard de React.

### ⚙️ LogicAgent (Algoritmos)
* **Skills:** Síntesis de código, diseño de estructuras de datos e implementación de lógica de negocio.
* **MCPs / Tools:**
    * `mcp_file_writer`: Creación y modificación segura de código fuente en el workspace actual.
    * `terminal_executor`: Ejecuta scripts de validación en la terminal integrada de VS Code.

### 🧹 RefactorAgent (Optimizador)
* **Skills:** Reducción de deuda técnica, aplicación de SOLID y optimización de Time/Space complexity.
* **MCPs / Tools:**
    * `ide_linter_runner`: Dispara y lee los resultados del linter activo del usuario (Ruff, ESLint, etc.).
    * `ast_analyzer`: Analiza el Abstract Syntax Tree de los archivos `.py` o `.ts` para reestructuración.

### 🛡️ SecOpsAgent (El Ciber-Guardia)
* **Skills:** Análisis Estático de Seguridad (SAST), detección de secretos (API keys), validación contra vulnerabilidades críticas (OWASP). Actúa como Gatekeeper antes del commit.
* **MCPs / Tools:**
    * `secret_scanner`: Escanea el código en memoria buscando patrones de credenciales, tokens JWT o claves expuestas.
    * `sast_runner`: Ejecuta herramientas de seguridad locales (ej. Bandit, Semgrep) y reporta mitigaciones.

### 🐛 DebugAgent y 🧪 TestAgent (Diagnóstico y QA)
* **Skills:** Root Cause Analysis (RCA), trazabilidad, creación y validación de pruebas unitarias/E2E.
* **MCPs / Tools:**
    * `log_ingester`: Lectura del output de la terminal en caso de crash o error de ejecución.
    * `test_framework_runner`: Ejecuta Pytest/Jest usando la configuración del entorno local.

### 🏗️ InfraAgent (El Operador DevOps)
* **Skills:** Contenerización, orquestación básica, manejo de variables de entorno y creación de pipelines CI/CD.
* **MCPs / Tools:**
    * `dockerfile_linter`: Valida que los manifiestos de Docker generados sigan las mejores prácticas (multi-stage, non-root).
    * `env_template_generator`: Extrae variables del código y crea/actualiza automáticamente archivos `.env.example`.

### 📖 DocAgent (Documentador)
* **Skills:** Mantenimiento de la memoria en texto plano, generación de docstrings explicativos y actualización del `README.md`.
* **MCPs / Tools:**
    * `markdown_memory_writer`: Escribe los nodos en formato `.md` para la base de datos GraphRAG.
---

## 12. Lógica del "Context Meter" y Smart Routing

El Dashboard de React en VS Code muestra en tiempo real el **Context Sufficiency Score (CSS)** y la decisión de enrutamiento del LLM.

**Fórmula de Precisión (CSS):**
`CSS = (0.5 * Semantic_Similarity) + (0.3 * Graph_Coverage) + (0.2 * Recency_Score)`

**Reglas de LangGraph basadas en CSS y Entropía:**
* **CSS < 40% (Alerta Roja):** Falla crítica de contexto. El Orchestrator enruta la tarea al modelo **CLOUD** para compensar con razonamiento puro o invoca a La Hormiga para pedir ayuda al usuario.
* **CSS > 75% + Tarea Simple (Verde):** Ejecución ultra-rápida. Se usa modelo **LOCAL SMALL**. Costo: $0.
* **CSS > 75% + Tarea Compleja (Verde):** Refactorización o lógica pesada. Se usa modelo **LOCAL BIG**. Costo: $0.

---

## 13. Flujo de Ejecución (Paso a Paso en LangGraph)

1.  **Input:** El programador escribe en el chat de la extensión: *"Crea el módulo de autenticación"*.
2.  **Environment Sync:** Se leen las preferencias de VS Code (ej. "Usa tabs, estilo Python Black").
3.  **GraphRAG Ingestion:** Se consulta LanceDB y NetworkX para extraer el contexto estructural de la petición.
4.  **Decisión de Router:** Se calcula el CSS y la complejidad. El Orchestrator asigna el LLM adecuado (Small/Big/Cloud).
5.  **Ejecución de Estado:** El `LogicAgent` procesa la solicitud mediante LangGraph, generando el código.
6.  **Validación:** El `RefactorAgent` y el `TestAgent` validan que se respeten las reglas del linter del usuario mediante llamadas MCP.
7.  **Output y Telemetría:** El código se inserta en el IDE y el Webview actualiza los KPIs (tokens ahorrados, latencia, modelo utilizado y mapa del grafo modificado).

---

## 14. Interacción con la "Hormiga" (AnalystAgent)

El **AgentMemory** suministra la materia prima estructural, pero es el **AnalystAgent** quien la interpreta y expone al humano. 
* Cuando el usuario pregunta: *"¿Por qué está fallando esta dependencia?"*, el AgentMemory usa el `dependency_mapper` para entregar el grafo subyacente. La Hormiga lo cruza con el perfil de entorno del IDE y proporciona una respuesta socrática y precisa, renderizada en el Dashboard de React.

---

## 15. Diseño de Interfaz y UI/UX (Architecture Layout)

El ecosistema visual de Ialienant está diseñado para minimizar la fricción del desarrollador, dividiéndose en dos entornos principales: el **Sidebar** (interacción continua) y el **Webview Dashboard** (supervisión y métricas). Todo bajo un sistema de diseño "Dark Mode First".

### 🎨 Sistema de Diseño y Paleta de Colores
| Elemento | Color (Hex/Nombre) | Propósito y Uso |
| :--- | :--- | :--- |
| **Fondo Principal** | `#1E1E1E` (Dark Slate) | Fondos de paneles y áreas de trabajo (alineado con VS Code default). |
| **Fondo Secundario** | `#252526` (Charcoal) | Tarjetas, bloques de código, y separadores de chat. |
| **Acento Principal** | `#007ACC` (Electric Blue) | Botones primarios, enlaces, y nodos principales en el grafo. |
| **Éxito / Local** | `#10B981` (Emerald Green) | Indicador de uso de LLM Local (Costo $0), Contexto alto (CSS > 75%), Tests pasados. |
| **Advertencia / Cloud**| `#F59E0B` (Amber) | Uso de LLM Cloud (Gasto de tokens), CSS Medio, Alertas de Code Drift. |
| **Peligro / Seguridad**| `#EF4444` (Crimson Red) | Fallos del `SecOpsAgent`, Errores críticos, CSS muy bajo. |
| **Texto Principal** | `#CCCCCC` (Light Gray) | Párrafos, descripciones legibles. |

---

### 📱 1. VS Code Sidebar: "Ailienant Command Chat"
Ubicado en el panel lateral izquierdo de VS Code. Es el punto de contacto principal con "La Hormiga" y el Orchestrator.

* **Cabecera (Sticky Header):**
  * **Logo & Título:** "Ialienant" con un icono de hormiga minimalista.
  * **Context Meter (Barra horizontal):** Una barra de progreso delgada debajo del título. Se llena de color Verde, Ámbar o Rojo dependiendo del *Context Sufficiency Score (CSS)* en tiempo real.
* **Área de Chat (Scrollable):**
  * **Burbujas de Usuario:** Alineadas a la derecha, fondo `#2D2D2D`.
  * **Burbujas de Agente:** Alineadas a la izquierda, fondo `#252526`. Muestran un avatar pequeño indicando qué agente está respondiendo (ej. 🧭 Planner, ⚙️ Logic, 🐜 Analyst).
  * **Bloques de Código:** Con botón de "Copy" y "Apply at Cursor" integrado en la esquina superior derecha del bloque.
* **Área de Input (Sticky Footer):**
  * **Caja de Texto:** Multilínea con auto-crecimiento.
  * **Model Toggle (Switch):** Un selector rápido justo encima de la caja de texto: `[Auto Router] | [Force Local] | [Force Cloud]`.
  * **Botones de Acción:** Botón de "Enviar" (Icono de flecha, Electric Blue) y botón de "Atajo de Contexto" (`@` para etiquetar archivos específicos manualmente).

---

### 🖥️ 2. Webview Dashboard: "The Control Room"
Se abre como una pestaña principal dentro del editor de VS Code (ocupando el espacio de un archivo). Construido en React + Vite.

* **Top Navigation Bar:**
  * **Pestañas:** `[ 🧠 Memory Graph ]`, `[ 📊 Telemetry & KPIs ]`, `[ ⚙️ Settings & MCPs ]`.
* **Sección 1: Memory Graph (Visualizador GraphRAG)**
  * **Lienzo Interactivo (React Flow):** Ocupa el 80% de la pantalla. Muestra los archivos `.md` y `.py/.ts` como nodos (círculos) conectados por líneas (dependencias).
  * **Colores de Nodos:** Azul (Archivos estables), Naranja (Archivos con "Code Drift" que necesitan re-indexación), Rojo (Nodos con vulnerabilidades detectadas).
  * **Panel Lateral de Detalles:** Al hacer clic en un nodo, se desliza un panel mostrando sus dependencias, el último hash indexado y un botón para "Forzar Re-indexación".
* **Sección 2: Telemetry & KPIs (Métricas)**
  * **Tarjetas de Resumen (Top):** * "Tokens Locales Ahorrados" (Verde).
    * "Gasto Cloud Estimado ($)" (Ámbar).
    * "Latencia Promedio" (Gris).
  * **Gráfico de LangGraph:** Un diagrama de flujo en vivo que se ilumina mostrando en qué estado se encuentra la petición actual (Planificación -> Orquestación -> Lógica -> Tests -> Seguridad).
* **Sección 3: Settings & MCPs (Configuración)**
  * **Lista de MCPs Activos:** Tarjetas rectangulares mostrando el estado de las conexiones (ej. `PostgreSQL Server: 🟢 Online`, `GitHub API: 🔴 Offline`). Botón para "Añadir nueva Tool".
  * **Configuración de Entorno:** Checkboxes para habilitar/deshabilitar la lectura de preferencias de VS Code (Linters, Temas, Tabulaciones).

  ### 🎯 3. Agent Launcher (Bento Menu / Selección Manual)
Para acelerar el flujo de trabajo de los *power users* y evitar latencias innecesarias de orquestación, el Sidebar incorpora un sistema de selección explícita de agentes.

* **Icono y Ubicación:** Un icono de cuadrícula de 3x3 (estilo Bento Menu / Google App Launcher) situado estratégicamente en la esquina inferior izquierda de la caja de texto del input.
* **Despliegue Visual:** Al hacer clic, se abre un pop-up superpuesto (Grid Layout) mostrando los avatares/iconos de los agentes especializados (ej. ⚙️ Logic, 🧹 Refactor, 🛡️ SecOps, 📖 Doc).
* **Flujo de Ejecución Dual:**
  * *Modo Auto (Default):* El usuario escribe su prompt y el `OrchestratorAgent` adivina y enruta la intención dinámicamente.
  * *Modo Manual (Direct Bypass):* El usuario selecciona un agente de la cuadrícula, forzando la atención del sistema. El prompt introducido se envía directamente a ese agente, reduciendo el consumo de tokens de enrutamiento y acelerando el tiempo de respuesta.

  ---

  ## 16. Modelo de Seguridad e "Invisible Sandboxing"

Para garantizar que los agentes autónomos no comprometan el entorno local del desarrollador al probar o compilar código, Ialienant implementa un modelo de seguridad de ejecución en tres capas:

* **📦 Ephemeral Execution (Sandboxing Invisible):** Cuando el `LogicAgent` o el `TestAgent` necesitan validar lógica, no lo hacen directamente en el host. El sistema utiliza el MCP para levantar un contenedor Docker efímero en segundo plano (o un entorno aislado WASM para ecosistemas Node). El agente inyecta el código, ejecuta los tests, captura el `stdout`/`stderr` y el contenedor se autodestruye. Todo este proceso dura milisegundos y es completamente transparente para el usuario.
* **🛡️ Human-in-the-Loop (HitL) Command Gate:** Para acciones que obligatoriamente deben interactuar con el entorno host (ej. `npm install`, operaciones de `git`, o modificaciones de infraestructura), la extensión bloquea la ejecución automática. El comando propuesto aparece en el Sidebar de VS Code esperando un clic de aprobación del usuario, evitando cambios indeseados en el sistema de archivos.
* **⏱️ Resource Limiters (Anti-Crash):** El entorno de ejecución tiene límites estrictos de CPU (timeouts de 10 segundos) y RAM (ej. max 512MB por tarea agentic). Si un modelo genera código con un bucle infinito o fugas de memoria, el sandbox "mata" el proceso y devuelve el error al `DebugAgent` para que lo solucione, garantizando que el IDE nunca se congele.
* **Nota de Resiliencia Arquitectónica (Circuit Breaker):** Para evitar bucles infinitos de ejecución o consumo excesivo de tokens durante la orquestación recursiva de LangGraph, Ialienant implementa un *Execution Circuit Breaker*. Si una tarea en proceso de validación (por ejemplo, entre el `SecOpsAgent` y el `LogicAgent`) supera el umbral de 3 ciclos de re-intento sin éxito, el sistema pausa la ejecución automáticamente. En este punto, el `AnalystAgent` (La Hormiga) entra en modo "Socrático", forzando una intervención manual del desarrollador (Human-in-the-Loop) para resolver la ambigüedad lógica antes de reanudar el flujo.

---

## 17. Persistencia y Fontanería de Datos (Data Plumbing)

Para garantizar una experiencia fluida, resiliente y respetuosa con la privacidad de los datos, el sistema implementa los siguientes protocolos a nivel de infraestructura:

* **⚡ Streaming Bidireccional (WebSockets/SSE):** La comunicación entre la extensión de VS Code (Frontend) y el servidor FastAPI (Backend) no es estática. Se utilizan WebSockets para el *streaming* de tokens en tiempo real (efecto máquina de escribir en el chat), actualización en vivo del GraphRAG en la UI y transmisión instantánea de alertas de seguridad del `SecOpsAgent`.
* **💾 LangGraph Checkpointing (Persistencia de Estado):** El flujo de los agentes se respalda continuamente en una base de datos SQLite local usando los *Checkpointers* nativos de LangGraph. Si el IDE se reinicia o se cierra inesperadamente, el usuario puede reanudar la sesión exactamente en el nodo donde se quedó (ej. continuar desde el paso 3 del plan del `PlannerAgent`).
* **🛑 Privacidad Granular (`.ailienantignore`):** Inspirado en Git, el sistema respeta un archivo `.ailienantignore` en la raíz del proyecto. Los archivos o directorios listados aquí pueden ser excluidos completamente de la memoria, o marcados con la etiqueta `local-only`, lo que instruye al `Orchestrator` a que, sin importar cuán bajo sea el CSS, esos archivos **jamás** sean enviados a los LLMs del Cloud, procesándose exclusivamente mediante los modelos de hardware local.

---

## 18. 🔮 Blindaje al Futuro (Future-Proofing)

Para garantizar la relevancia de **Ailienant** ante la evolución acelerada de los LLMs, el proyecto contempla una hoja de ruta de capacidades avanzadas:

* **👁️ Multi-Modalidad de Entrada:** Evolución del `AnalystAgent` para procesar capturas de pantalla de errores de UI o diagramas manuales directamente desde VS Code, utilizando modelos de visión para diagnosticar problemas de frontend.
* **🧠 Aprendizaje de Refuerzo Local (Memory Tuning):** Implementación de un sistema de retroalimentación donde, si el desarrollador corrige manualmente el código generado, el `AgentMemory` almacena esa corrección como un "ejemplo positivo" (Few-shot learning), evitando la repetición de errores similares.
* **🤝 Agentes Colaborativos P2P (Post-MVP):** > *Nota: Esta funcionalidad está proyectada para versiones futuras después del Producto Mínimo Viable (MVP).*
    > Permitirá que instancias de Ailienant se comuniquen entre compañeros de equipo de forma descentralizada (Peer-to-Peer) para compartir el conocimiento del grafo del proyecto y descubrimientos de arquitectura sin depender de un servidor central.
* **💻 Hardware Awareness:** Optimización dinámica del orquestador para detectar automáticamente la capacidad de cómputo local (NVIDIA CUDA o Apple Silicon). Esto permitirá seleccionar el modelo "Local Big" más pesado que el hardware pueda ejecutar sin comprometer la latencia de la IDE.
* **🚀 Visión a Futuro: Ialienant Enterprise** > *Nota: Esta funcionalidad está proyectada para versiones futuras después del Producto Mínimo Viable (MVP).*

Mientras que el núcleo de Ialienant siempre será Open Source y Local-First para el desarrollador individual, la arquitectura está diseñada para escalar hacia un modelo **Enterprise On-Premise (BYOC - Bring Your Own Cloud)** orientado a equipos de ingeniería corporativos.

Esta evolución se centrará en tres pilares de monetización y escalabilidad:

1.  **GraphRAG Colaborativo (Memoria de Enjambre):**
    * Implementación de LanceDB centralizado dentro de la VPC de la empresa. Si un desarrollador en el equipo de Backend resuelve un bug y el agente de Ialienant lo indexa, el equipo de Frontend recibe de inmediato el contexto topológico de esa solución, reduciendo los silos de información.
    * *Complejidad de Búsqueda:* Mantenida en $O(\log n)$ utilizando HNSW (Hierarchical Navigable Small World) sobre la red local, asegurando latencia < 10ms.

2.  **FinOps & AI Governance Dashboard:**
    * Un panel de control administrativo (Control Room) donde los CTOs y Tech Leads pueden establecer políticas de enrutamiento (ej. "Solo el equipo Senior puede usar Claude Opus, el resto utilizará modelos locales para tareas de bajo CSS").
    * Auditoría en tiempo real del gasto de tokens y la huella computacional del equipo.

3.  **Seguridad Zero-Trust (Air-Gapped):**
    * Despliegue autohospedado (Docker/Kubernetes). Ailienant Enterprise operará 100% detrás del firewall corporativo, garantizando que el código propietario jamás se transmita a servidores de terceros, resolviendo el mayor impedimento legal para la adopción de herramientas agentic en el sector financiero y de salud.

---

## 19. 🛡️ Gestión de Riesgos y Resiliencia Técnica

Para asegurar que Ialienant mantenga un alto rendimiento y no sufra degradación en entornos locales o de alta complejidad, la arquitectura implementa los siguientes protocolos de mitigación de riesgos:

* **Motor de Inferencia 3D (CSS, TCI y Capacidad):** El enrutamiento de tareas no depende de una sola variable, sino de una matriz tridimensional de evaluación rápida. Primero, se calcula la suficiencia del contexto ($CSS$) mediante una **Función de Puntuación Ponderada** $O(1)$: `CSS = (w1 * Cosine_Similarity) + (w2 * Graph_Density) + (w3 * Time_Decay)`. Simultáneamente, un escaneo heurístico de complejidad $O(N)$ evalúa el requerimiento cognitivo del prompt para generar un **Task Complexity Index (TCI)**. Finalmente, el Orquestador cruza estos datos con el Perfil de Hardware disponible. Si la tarea tiene bajo TCI y alto CSS, se aplica un "Fast Track" que redirige a modelos locales ultrarrápidos, priorizando el *Time To First Token* (TTFT) sin comprometer la precisión.
* **Prevención de Code Drift:** Dado que los desarrolladores pueden alterar el código sin usar a Ialienant, se ha diseñado un *Save Hook* a nivel IDE (VS Code). Cada vez que se guarda un archivo crítico, se dispara una indexación atómica en segundo plano para sincronizar la memoria GraphRAG, evitando discrepancias cognitivas.
* **Sobrecarga de Renderizado UI:** Para prevenir que el Webview colapse al visualizar proyectos enormes, la interfaz de grafos (React Flow) utiliza técnicas de **Virtualización y Nivel de Detalle (LOD)**, renderizando únicamente los clústeres de memoria adyacentes a la tarea activa.
* **Degradación Elegante y Strict Local Mode:** Si un servidor MCP local falla, los agentes operan en modo restringido sin abortar la sesión. En caso de hardware limitado (ej. < 16GB VRAM) o un CSS deficiente, el enrutador evalúa la directiva `is_strict_local`. Si el usuario opera en entornos Air-gapped o corporativos (Strict Mode = True), el sistema **nunca** filtrará código a la nube; en su lugar, ejecutará una *Degradación Elegante* (ej. solicitando refinar el prompt o reducir el scope). Solo si el modo estricto está desactivado, el 'Smart Routing' escalará a modelos Cloud (Claude/GPT-4o) como medida de emergencia.

## 20. 🚀 Roadmap: Ialienant Enterprise (Monetization & B2B)

El MVP actual de Ialienant está diseñado como un **Fat Client** hiper-optimizado para ejecución 100% local, garantizando privacidad y autonomía sin costo de infraestructura. Sin embargo, para escalar en entornos corporativos y equipos de alto rendimiento, nuestra hoja de ruta incluye el despliegue de **Ialienant Enterprise**, una arquitectura de **Thin Client + Centralized Brain** diseñada para resolver los cuellos de botella organizacionales.

Esta transición nos permitirá ofrecer licencias B2B, soporte premium y despliegues On-Premise a través de las siguientes características clave:

---

### 1. Despliegue Corporativo "Plug & Play" (IaC)

Para eliminar la fricción en los departamentos de TI, empaquetaremos el backend de Ialienant para entornos de servidor.

* **Docker & Helm Charts:** Despliegue de la base de datos de grafos, LanceDB y el motor de orquestación con un solo comando en la nube privada de la empresa (AWS, GCP, Azure) o en clústeres de Kubernetes On-Premise.
* **Aislamiento Total:** El código corporativo nunca sale de la VPC (Virtual Private Cloud) de la empresa.

### 2. CI/CD GraphRAG Indexing (Zero "Cold Starts")

El costo de indexación masiva se elimina de las laptops de los desarrolladores.

* **Integración Nativa (GitHub / GitLab Apps):** Un Webhook escuchará los *merges* en las ramas principales y un servidor dedicado recalculará el Abstract Syntax Tree (AST) y el *Personalized PageRank* en segundo plano.
* **Sincronización Diferencial:** Cuando un desarrollador corporativo abre VS Code, la extensión descarga instantáneamente el "Cerebro" (Snapshot del Grafo) actualizado. El "Cold Start" pasa de minutos a milisegundos.

### 3. Control Plane y Seguridad (Panel de Administración)

Un dashboard web para que los Directores de TI y líderes técnicos gestionen la plataforma:

* **Gestión de Accesos (RBAC):** Restricción de contexto por equipos (ej. El equipo de Frontend no puede consultar el contexto GraphRAG del módulo de procesamiento de pagos).
* **Auditoría y Costos (FinOps):** Monitoreo en tiempo real del consumo de tokens y utilización de cómputo por usuario y departamento.

### 4. Bring Your Own Model (BYOM) & Fleet Management

Liberaremos a los desarrolladores de las limitaciones de su hardware local.

* **Granjas de Inferencia Internas:** Ialienant Enterprise podrá conectarse directamente a clústeres internos de vLLM/Ollama (ej. flotas de servidores con GPUs H100) o a APIs corporativas securizadas (Azure OpenAI, AWS Bedrock).
* **VS Code "Enterprise Mode":** Un simple *switch* en la extensión que apaga el procesamiento local, libera el 100% de la RAM del desarrollador y enruta la telemetría y los prompts hacia el servidor central con latencia cercana a cero.

### 5. Enterprise Data Layer (Transición a Supabase)
Para soportar la concurrencia masiva y la seguridad corporativa, el backend migrará de nuestra solución local embebida (SQLite + LanceDB) a una infraestructura Cloud-Native basada en **Supabase (PostgreSQL)**.

* **Búsqueda Vectorial Unificada:** Uso nativo de `pgvector` para almacenar metadatos, Abstract Syntax Trees (AST) y *embeddings* matemáticos en la misma transacción, simplificando la arquitectura y acelerando las consultas del Grafo.
* **Seguridad desde la Raíz (RLS):** Implementación de *Row Level Security* (Seguridad a Nivel de Fila) nativa de Postgres. Esto garantiza criptográficamente que la IA solo procese y responda con contexto de los repositorios a los que el empleado tiene acceso explícito.
* **Sincronización de Grafo en Tiempo Real:** Reemplazo del *polling* tradicional por *Supabase Realtime* (vía WebSockets), permitiendo que cuando el servidor de CI/CD actualice el monorepo, el "Cerebro Central" empuje los nuevos vectores directamente a la extensión de VS Code de todo el equipo instantáneamente.