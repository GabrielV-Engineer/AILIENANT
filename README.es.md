<!-- markdownlint-disable MD033 MD041 -->
<div align="center">

<img src="assets/icon-color.svg" alt="AILIENANT" width="340" />

<h1>AILIENANT</h1>

<p><strong>El compañero de programación con IA que planifica antes de programar — y funciona en tu máquina, con tus modelos y según tus reglas.</strong></p>

<p>
  <a href="README.md">English</a> ·
  <strong>Español</strong> ·
  <a href="README.fr.md">Français</a> ·
  <a href="README.zh.md">中文</a> ·
  <a href="README.hi.md">हिन्दी</a> ·
  <a href="README.ru.md">Русский</a> ·
  <a href="README.it.md">Italiano</a>
</p>

<p>
  <a href="LICENSE"><img alt="Licencia: AGPL-3.0" src="https://img.shields.io/badge/License-AGPL%20v3-blue.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="TypeScript" src="https://img.shields.io/badge/TypeScript-5.9-3178C6?logo=typescript&logoColor=white">
  <img alt="VS Code" src="https://img.shields.io/badge/VS%20Code-Extensi%C3%B3n-007ACC?logo=visualstudiocode&logoColor=white">
  <img alt="Estado" src="https://img.shields.io/badge/estado-en%20desarrollo%20activo-success">
</p>

</div>

---

## ¿Qué es AILIENANT?

**AILIENANT es un agente de programación autónomo que vive dentro de VS Code.** Describes lo que quieres en lenguaje natural; AILIENANT escribe un plan real, hace los cambios, ejecuta el código en un entorno aislado, lee los resultados y corrige sus propios errores — todo mientras te muestra cada paso de su razonamiento.

Lo que lo distingue de los asistentes de IA más populares es **dónde se ejecuta y cómo decide.** AILIENANT es **local-first**: puede funcionar por completo en tu propia máquina con modelos abiertos (Ollama, LM Studio y otros), recurriendo a la nube solo cuando una tarea realmente lo necesita — y te lo dice, en dólares, cuando lo hace. Tu código no tiene por qué salir de tu portátil, y nunca quedas atado a un único proveedor.

Tu código merece más que autocompletado mágico o agentes autónomos que operan a ciegas. Ailienant es el único agente para VS Code construido para ingenieros que exigen rigor, auditabilidad y control total.

Operamos bajo la filosofía de Spec-Driven Development. Ailienant lee tu arquitectura como un grafo vivo (GraphRAG) y utiliza razonamiento de última generación — Dreaming, MCTS (Monte Carlo Tree Search) y Harness Systems — para planificar y ejecutar tareas complejas en múltiples archivos. Esto garantiza una precisión milimétrica y muchos menos errores, incluso superando las limitaciones de contexto de los modelos locales en PCs.

Todo está integrado de forma fácil de usar, pero sin ser una "caja negra". Tú dictas las reglas: elige el modelo, cambia el motor de ejecución, controla el presupuesto y audita cada paso del pipeline. Ailienant ejecuta de forma autónoma, pero pausa para tu aprobación antes de cualquier acción crítica (HITL). Verdadera autonomía, cero dependencia de proveedores. El pipeline es tuyo.

> **En una línea:** un ingeniero de IA privado, consciente del coste y que planifica primero, para tu código — open source y sin dependencia de un proveedor.

---

## Por qué la gente lo usa

- **🧠 Planifica antes de programar.** Un verdadero equipo de agentes especializados — un *Investigador* mapea tu código, un *Planificador* convierte la petición en una especificación concreta y una lista de tareas y congela el alcance, un *Orquestador* dirige los pasos, un *Programador* (en uno de 8 roles expertos) hace los cambios, y un *Analista* con el que puedes conversar explica el código. Una guardia de desviación impide que el agente se desvíe en silencio y reescriba medio proyecto.
- **🔒 Tu código sigue siendo tuyo.** Funciona 100% en local con tus propios modelos. Sin nube obligatoria, sin telemetría que llama a casa, sin entrenar con tu repositorio.
- **💸 Ves el coste.** Cada tarea tiene un registro de tokens en vivo y un límite de presupuesto estricto. El uso local frente al de nube y el ahorro estimado se muestran, no se ocultan.
- **🪟 Ves el razonamiento.** Una "Caja de Pensamiento" en vivo transmite el razonamiento del modelo, y una traza paso a paso muestra cada archivo leído, comando ejecutado y parche propuesto.
- **⏪ Puedes rebobinar.** Cada paso de una tarea es un punto de control duradero. Ramifica desde cualquier punto para explorar una alternativa — depuración con viaje en el tiempo, de verdad, para un agente.
- **🛡️ Ejecuta código de forma segura.** Los comandos generados se ejecutan en un entorno aislado (Docker, con alternativas de WebAssembly y de aprobación humana), nunca a ciegas contra tu máquina.
- **🔌 Sin ataduras.** Trae tu propio modelo y proveedor — Ollama, LM Studio, vLLM, llama.cpp, OpenAI, Anthropic, Google, DeepSeek, Mistral y más — y cámbialo cuando quieras.

---

## ¿En qué se diferencia?

| | **AILIENANT** | Asistente de nube típico |
| --- | --- | --- |
| Funciona del todo en tu máquina | ✅ Local-first, modelo propio | ❌ Solo nube |
| Investiga, planifica, programa y se autoverifica | ✅ Un equipo de 5 agentes con guardia de desviación | ❌ Un modelo, un intento |
| Enrutado inteligente local↔nube | ✅ Elige el nivel más barato que sirva | ❌ Fijo |
| Muestra el coste en tiempo real | ✅ Registro de tokens + límite de presupuesto | ⚠️ Normalmente oculto |
| Viaje en el tiempo / ramificar una ejecución | ✅ Puntos de control duraderos | ❌ Sin estado |
| Ejecución aislada | ✅ Docker / Wasm / con aprobación | ⚠️ A menudo en el host |
| Dependencia de proveedor | ✅ Ninguna — cambia libremente | ❌ Atado a uno |

Una comparación técnica más completa está en **[HowItWorks.md](HowItWorks.md)**.

---

## El equipo por dentro

AILIENANT no es un único modelo haciéndolo todo — es un pequeño equipo de especialistas, cada uno con una tarea, conectados por un motor **LangGraph** con estado:

| Agente | Qué hace |
| --- | --- |
| 🔭 **Investigador** | Construye un "mapa esqueleto" de tu código — firmas y relaciones entre módulos — para que el Planificador razone sobre la estructura real, no sobre conjeturas. |
| 🧭 **Planificador** | Convierte tu petición en una especificación concreta y validada y una lista de tareas (una WBS), y luego **congela el alcance** para que el trabajo no se desborde. |
| 🎛️ **Orquestador** | Dirige el plan paso a paso, coordinando el estado y enrutando cada paso al nivel de modelo adecuado. |
| 🛠️ **Programador** | Hace los cambios reales — adoptando uno de **8 roles expertos** por tarea. |
| 💬 **Analista (Natt)** | Un tutor de solo lectura con el que puedes conversar. Explica tu código y al propio AILIENANT, pero nunca toca archivos — la *voz*, no la *mano*. |

El Programador se especializa en el rol que cada tarea necesita: **core-dev, arquitecto/refactor, devops/infra, secops, qa-tester, doc-manager, vcs-manager, ingeniero de datos/ML** — cada uno con sus propias herramientas, salvaguardas y disparadores de aprobación (p. ej., una edición de `.env` siempre se pausa para ti).

Cuando un paso falla, un bucle de **autocuración** lee el error y propone un parche corregido antes de rendirse; para pasos abiertos, una **célula ReAct** acotada trabaja contra una terminal en vivo hasta terminar el trabajo. El desglose completo por agente está en **[HowItWorks.md](HowItWorks.md)**.

---

## Seguridad y protección, por diseño

AILIENANT asume que, tarde o temprano, un agente autónomo intentará hacer algo que no debería — y está construido para contenerlo.

- **Aislado por defecto.** Los comandos se ejecutan en un contenedor Docker aislado (workspace de solo lectura, sin red, sin root) con alternativas de WebAssembly y con intervención humana cuando Docker no está disponible.
- **Permisos fail-closed.** Cada herramienta se clasifica por privilegio; cualquier cosa no reconocida se trata como **peligrosa hasta demostrar lo contrario**, nunca al revés.
- **Aprobación humana donde importa.** Las acciones arriesgadas y los excesos de presupuesto se pausan para tu aprobación explícita.
- **Registro de auditoría a prueba de manipulaciones.** Las aprobaciones se registran en un libro encadenado criptográficamente (blake2b) que puedes verificar.
- **Aislamiento multi-inquilino.** Cada fragmento de memoria indexada se asocia a su workspace, así los proyectos nunca se filtran entre sí.

---

## Inicio rápido

> Guía completa: **[HowToUseIt.md](HowToUseIt.md)**

**Requisitos previos:** Python 3.10+ (3.13 recomendado), Node.js 20+, VS Code 1.85+ y al menos una fuente de modelos (una instalación local de Ollama/LM Studio, un proxy [LiteLLM](https://docs.litellm.ai/docs/simple_proxy), o claves de API en la nube).

```powershell
# 1. Backend (el motor de orquestación)
cd ailienant-core
python -m venv venv
.\venv\Scripts\activate          # Unix: source venv/bin/activate
pip install -r requirements.txt
copy ..\.env.example ..\.env     # Unix: cp ../.env.example ../.env

# 2. Extensión (la interfaz de VS Code)
cd ..\ailienant-extension
npm install
npm run compile
```

Luego abre el proyecto en VS Code y pulsa **F5** para lanzar la extensión. La primera vez que abras una sesión de AILIENANT, **arranca el backend por ti en un puerto local asignado automáticamente** (un puerto `127.0.0.1` libre, p. ej. `http://127.0.0.1:59247/`) y conecta la interfaz a él — no hay puerto que configurar. Después empieza a indexar tu workspace. Configura tus modelos desde el panel **BYOM** integrado, escribe una petición y listo.

> ¿Ejecutas el backend a mano (sin interfaz / CI)? Lánzalo con `uvicorn main:app --port 8000` y apunta el ajuste `backendUrl` de la extensión hacia él. El puerto asignado automáticamente es solo para el flujo normal dentro de VS Code.

---

## Cómo funciona (versión corta)

```
Preguntas ─▶ Investigador ─▶ Planificador ─▶ guardia ─▶ Programador ─▶ el sandbox lo ejecuta
             (mapea el        (spec +         desviación  (edita          ▲      │
              código)          plan)          (bloqueo)    archivos)       │      ▼
                                                                    autocuración ◀─ lee el resultado
```

Por dentro, un motor **LangGraph** con estado enruta cada tarea entre modelos locales y de nube usando una puntuación de contexto y complejidad — eligiendo siempre el **nivel más barato capaz de hacer el trabajo** y recurriendo a la nube solo cuando una tarea realmente lo necesita.

Recupera los archivos correctos con **GraphRAG**: en lugar de volcar archivos enteros en el prompt, indexa tu código como un grafo de dependencias (Tree-sitter) con embeddings vectoriales, y luego extrae solo el fragmento relevante mediante búsqueda vectorial + un recorrido de dependencias de k saltos ordenado por importancia (PageRank). Eso mantiene los prompts pequeños — una **reducción media del ~70 % del tamaño del prompt** — que es justo lo que permite a AILIENANT **funcionar bien en hardware modesto**: los presupuestos por nivel mantienen el contexto dentro de la ventana de un modelo local pequeño (de tan solo 4 K tokens), y el índice vive en un almacén rápido y en RAM. Cada paso tiene un punto de control para no perder nada.

**Construido sobre una especificación, no sobre suposiciones.** Antes de tocar cualquier archivo, el Planificador convierte tu solicitud en una `MissionSpecification` congelada — resultado esperado, alcance, pasos del WBS, restricciones y criterios de aceptación (incluyendo terminología TDD y DDD). Una vez congelada, ni el Planificador ni el Programador pueden cambiar el alcance silenciosamente: un `drift_monitor` compara cada replanificación con el original usando una métrica de similitud multi-factor y te consulta si detecta desviación. La especificación es el contrato; el agente no puede autoautorizarse cambios de alcance.

**Los fallos se enrutan, no se convierten en crashes.** Cada turno de agente se ejecuta dentro de un harness de ejecución estructurado: un `reflexion_guard` intercepta las excepciones y las enruta a un agente de reparación dedicado (en lugar de mostrar un traceback), un `finops_gate` determinista aplica tu límite de coste en cada paso del grafo, y veredictos estructurados — no stdout crudo — impulsan todas las decisiones de reintento. Si un nodo tiene una excepción no manejada, se escribe en una cola de mensajes fallidos antes de propagar el error para que puedas inspeccionar y reanudar.

La versión profunda — diagramas, el esquema completo de la especificación y la lógica de enrutado de reparación — está en **[HowItWorks.md](HowItWorks.md)**.

---

## Conversa con tu código: el Analista

No toda pregunta necesita que el agente *haga* algo — a veces solo quieres entender. El **Analista (Natt)** es un compañero de chat que vive en un panel lateral: pregúntale *"¿cómo fluye la autenticación por este servicio?"*, *"¿qué se rompería si cambio esta función?"* o incluso *"¿cómo funciona realmente el enrutado de AILIENANT?"* y te responde en lenguaje claro.

Es un **tutor de solo lectura — la voz, nunca la mano.** Explica, traza y enseña, pero nunca edita tus archivos, así que puedes explorar con libertad sin que cambie nada.

Lo que hace fiables sus respuestas es **en qué se basa** — tres fuentes a la vez: el **grafo de conocimiento** de tu código (para citar la estructura real, no una alucinación), el **README de tu workspace** (para conocer la intención de tu proyecto) y la **propia documentación de producto de AILIENANT** (para explicar la herramienta en sí). Y como explicar es más barato que programar, **eliges el modelo de respuesta** desde un pequeño selector — un modelo local rápido para preguntas rápidas, uno más potente para un recorrido arquitectónico profundo — sin afectar a la calidad de la recuperación.

---

## Memoria que puedes ver

La comprensión que AILIENANT tiene de tu código no es una caja negra. El **panel de control** integrado representa el índice GraphRAG como un **grafo de conocimiento interactivo** — un mapa dirigido por fuerzas de tus archivos y sus dependencias, donde los archivos "concentradores" más conectados destacan, los módulos relacionados comparten color y la importancia (PageRank) guía la disposición. Un **mapa vectorial** 2D acompañante proyecta cómo agrupa el motor tu código *semánticamente*. Es una imagen viva de lo que el agente sabe, y de cómo decide qué leer.

---

## Un ecosistema abierto

- **🧩 Servidores MCP.** AILIENANT habla el **Model Context Protocol**, con un registro curado de servidores verificados (GitHub, Brave Search, Docker, Postgres) que puedes activar con un clic. Cada herramienta MCP se **clasifica por privilegio** — las desconocidas se tratan como peligrosas hasta demostrar lo contrario — y solo se confían durante la sesión después de que tú las apruebes.
- **⚡ Skills.** Guarda fragmentos de instrucciones reutilizables — globales o por workspace — y suéltalos en cualquier prompt. Tus propias plantillas de comandos, versionadas con el proyecto.
- **🧰 Herramientas.** Los agentes actúan a través de un registro de herramientas tipado y restringido por rol: leer y trazar código, editar archivos transaccionalmente, ejecutar comandos en el sandbox y preguntarte cuando dudan. El catálogo está **creciendo hacia ~56 herramientas asignadas por rol** (ver la hoja de ruta en **[docs/PROJECT_MANIFEST.md](docs/PROJECT_MANIFEST.md)**); la tabla completa — qué agente usa qué herramienta — está en **[HowItWorks.md](HowItWorks.md)**.

---

## Dreaming: mejora mientras no estás

Programar es a ráfagas — sales a comer, te desconectas por la noche. El **Modo Dreaming** convierte ese tiempo inactivo en progreso. Le indicas a AILIENANT en qué pensar — *arquitectura y patrones*, *refactorización y deuda técnica*, *corrección de errores*, todo el workspace o un tema que escribas — y mientras no estás trabaja ese foco de forma autónoma: estudiando el código, **consolidando lo que aprende en la memoria a largo plazo** y explorando mejoras. Se autocorrige sobre la marcha y **se detiene solo si los errores empiezan a acumularse**.

Y lo más importante: **nunca se despierta por un temporizador para asaltar tu máquina** — *tú* decides cuándo gastar los recursos arrancándolo cuando te alejas. Tiene **límite de presupuesto** (se niega una vez alcanzado el techo de gasto de la sesión) y es seguro: si vuelves y guardas un archivo a mitad de una pasada, esa pasada se aborta limpiamente sin escribir.

Elige el **perfil** que encaje con el descanso que te tomas — intercambian velocidad, coste y profundidad:

| Perfil | Ideal para | Aproximadamente |
| --- | --- | --- |
| **Medium** | Una pausa de comida — ligero, totalmente local | 1 tarea · 3 archivos · ~60 min |
| **Big** | Toda la noche — más profundo, más archivos, local | 3 tareas · 10 archivos · nocturno |
| **Cloud** | Razonamiento de máxima calidad, acotado por tokens | 1 tarea · 5 archivos · con tope de tokens |
| **Hybrid** | La nube *planifica*, el modelo local *edita* — calidad a menor coste | 2 tareas · 6 archivos |

El mecanismo completo — qué puede lograr cada perfil, los tiempos estimados y cómo la búsqueda en árbol offline (MCTS) valida los cambios candidatos — está en **[HowItWorks.md](HowItWorks.md)**.

---

## Terminal en vivo y panel de control

El agente trabaja contra una **terminal persistente e interactiva** — una sesión de shell real que recuerda su directorio de trabajo y su entorno entre comandos, transmite la salida en vivo y puede interrumpirse — todo dentro del sandbox. El **panel de control** (un dashboard integrado, servido localmente) te da once vistas sobre una sesión en ejecución: telemetría de coste y enrutado, estado de hardware y runtime, el grafo de memoria, modelos BYOM, servidores MCP y skills, reglas de gobernanza, un área de staging para revisar parches pendientes, un libro de auditoría a prueba de manipulaciones y recuperación ante caídas.

---

## Documentación

| Documento | Para quién |
| --- | --- |
| **[HowToUseIt.md](HowToUseIt.md)** | Cualquiera — instalar, configurar y ejecutar tu primera tarea, paso a paso |
| **[HowItWorks.md](HowItWorks.md)** | Los curiosos — arquitectura, enrutado y modelo de seguridad explicados |
| **[DEVELOPERS.md](DEVELOPERS.md)** | Desarrolladores del núcleo — internos profundos, diagramas, pseudocódigo, mapa de código |
| **[CONTRIBUTING.md](CONTRIBUTING.md)** | Contribuidores — configuración, estándares y cómo enviar un buen PR |
| **[docs/PROJECT_MANIFEST.md](docs/PROJECT_MANIFEST.md)** | La hoja de ruta completa fase por fase |

---

## Contribuir

AILIENANT es open source y las contribuciones son bienvenidas — desde corregir una errata hasta cerrar un objetivo de la hoja de ruta. Empieza por **[CONTRIBUTING.md](CONTRIBUTING.md)**.

Algo que conviene saber de antemano: como el proyecto tiene licenciamiento dual (ver más abajo), cada contribuidor firma un breve **[Acuerdo de Licencia de Contribuidor (CLA)](CLA.md)** antes de fusionar su primer PR. Es un paso único y conservas el copyright de tu trabajo.

---

## Licencia

AILIENANT es **open-core y de licenciamiento dual**:

- **Edición Comunidad — [GNU AGPL-3.0](LICENSE).** Libre para usar, estudiar, modificar y compartir. Si la distribuyes o ejecutas una versión modificada como servicio de red, compartes tu código fuente bajo la misma licencia.
- **Edición Comercial / Enterprise.** Para organizaciones que no pueden aceptar los términos de la AGPL o que quieren funciones y soporte enterprise.

Consulta **[LICENSING.md](LICENSING.md)** para el panorama completo y cómo obtener una licencia comercial.

> El nombre **AILIENANT** y sus logotipos son marcas del proyecto y no están cubiertos por la AGPL.

---

<div align="center">

**Hecho para ingenieros que quieren un compañero de IA en el que de verdad puedan confiar — y auditar.**

Sobre los hombros de <a href="https://github.com/langchain-ai/langgraph">LangGraph</a> · <a href="https://lancedb.com/">LanceDB</a> · <a href="https://tree-sitter.github.io/">Tree-sitter</a> · <a href="https://github.com/BerriAI/litellm">LiteLLM</a> · <a href="https://docs.pydantic.dev/">Pydantic</a>.

</div>
