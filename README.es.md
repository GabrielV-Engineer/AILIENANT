<!-- markdownlint-disable MD033 MD041 -->
<div align="center">

<img src="assets/logo.svg" alt="AILIENANT" width="340" />

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

> **En una línea:** un ingeniero de IA privado, consciente del coste y que planifica primero, para tu código — open source y sin dependencia de un proveedor.

---

## Por qué la gente lo usa

- **🧠 Planifica antes de programar.** Un *Planificador* dedicado convierte tu petición en una especificación concreta y una lista de tareas, congela el alcance y vigila la "desviación" para que el agente no se desvíe en silencio y reescriba medio proyecto. Un *Programador* aparte ejecuta ese plan. Dos cabezas, cada una haciendo bien una tarea.
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
| Planifica y luego programa (bicéfalo) | ✅ Planificador + Programador, con guardia de desviación | ❌ Un modelo, un intento |
| Enrutado inteligente local↔nube | ✅ Elige el nivel más barato que sirva | ❌ Fijo |
| Muestra el coste en tiempo real | ✅ Registro de tokens + límite de presupuesto | ⚠️ Normalmente oculto |
| Viaje en el tiempo / ramificar una ejecución | ✅ Puntos de control duraderos | ❌ Sin estado |
| Ejecución aislada | ✅ Docker / Wasm / con aprobación | ⚠️ A menudo en el host |
| Dependencia de proveedor | ✅ Ninguna — cambia libremente | ❌ Atado a uno |

Una comparación técnica más completa está en **[HowItWorks.md](HowItWorks.md)**.

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

Luego abre el proyecto en VS Code y pulsa **F5** para lanzar la extensión. La primera vez que abras una sesión de AILIENANT, arrancará el backend por ti y empezará a indexar tu workspace. Configura tus modelos desde el panel **BYOM** integrado, escribe una petición y listo.

---

## Cómo funciona (versión corta)

```
Preguntas ─▶ Planificador ─▶ guardia de ─▶ Programador ─▶ el sandbox lo ejecuta
            (escribe spec    desviación     (edita            ▲      │
             + plan)         (alcance        archivos)        │      ▼
                              bloqueado)                  corrígelo ◀─ lee el resultado
```

Por dentro, un motor **LangGraph** con estado enruta cada tarea entre modelos locales y de nube usando una puntuación de contexto y complejidad, recupera los archivos correctos con **GraphRAG** (búsqueda vectorial + un recorrido de dependencias de un salto) y guarda un punto de control en cada paso para no perder nada. La versión profunda — diagramas, la matemática del enrutado, el bucle de ejecución y el modelo de seguridad — está en **[HowItWorks.md](HowItWorks.md)**.

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
