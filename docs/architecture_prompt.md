# Instrucciones de Arquitectura y Reglas de Modificación (System Prompt)

**Contexto Obligatorio:** A partir de este momento, toda la creación, edición, y refactorización de código para el ecosistema **AILIENANT** se basará estrictamente en las estructuras de directorios definidas a continuación. 

**Reglas de Operación:**
1. **Análisis de Existencia:** Antes de generar o sugerir código, debes analizar si el archivo solicitado ya existe en la estructura actual o si es un archivo que debe ser creado desde cero.
2. **Ubicación Estricta:** Si el archivo es nuevo o requiere modificación, debes examinar la estructura arquitectónica e indicar la **ruta exacta y específica** (ej. `src/api/contracts.ts` o `agents/planner.py`) donde debe ser creado o modificado *antes* de proporcionar el bloque de código. No improvises carpetas que no estén en este mapa.
3. **Simetría y Dominio:** Respeta la separación de responsabilidades dictada por las carpetas (`api`, `brain`, `core`, `editor`, `tools`, etc.).

---

## 1. Frontend: ailienant-extension

Esta estructura corresponde a la extensión de VS Code y el Webview (React) que actúan como la interfaz de usuario y el recolector de contexto del IDE.

```text
📦ailienant-extension
 ┣ 📂.vscode
 ┃ ┣ 📜extensions.json
 ┃ ┣ 📜launch.json
 ┃ ┣ 📜settings.json
 ┃ ┗ 📜tasks.json
 ┣ 📂media
 ┣ 📂src
 ┃ ┣ 📂api
 ┃ ┃ ┣ 📜api_client.ts
 ┃ ┃ ┣ 📜contracts.ts
 ┃ ┃ ┗ 📜ws_client.ts
 ┃ ┣ 📂brain
 ┃ ┃ ┣ 📜session.ts
 ┃ ┃ ┗ 📜state_manager.ts
 ┃ ┣ 📂editor
 ┃ ┃ ┣ 📜code_lens.ts
 ┃ ┃ ┣ 📜diagnostics.ts
 ┃ ┃ ┗ 📜vfs_reader.ts
 ┃ ┣ 📂providers
 ┃ ┃ ┣ 📜chat_sidebar.ts
 ┃ ┃ ┗ 📜commands.ts
 ┃ ┣ 📂shared
 ┃ ┃ ┣ 📜config.ts
 ┃ ┃ ┗ 📜logger.ts
 ┃ ┣ 📂test
 ┃ ┃ ┗ 📜extension.test.ts
 ┃ ┣ 📂webview
 ┃ ┃ ┣ 📂components
 ┃ ┃ ┃ ┣ 📜AudioVisualizer.tsx
 ┃ ┃ ┃ ┣ 📜ChatBubble.tsx
 ┃ ┃ ┃ ┗ 📜Timeline.tsx
 ┃ ┃ ┣ 📜App.tsx
 ┃ ┃ ┗ 📜index.css
 ┃ ┗ 📜extension.ts
 ┣ 📜.vscode-test.mjs
 ┣ 📜.vscodeignore
 ┣ 📜CHANGELOG.md
 ┣ 📜esbuild.js
 ┣ 📜eslint.config.mjs
 ┣ 📜package-lock.json
 ┣ 📜package.json
 ┣ 📜README.md
 ┣ 📜tsconfig.json
 ┣ 📜vsc-extension-quickstart.md
 ┗ 📜webpack.config.js
```

 ---

 ## 2. Backend: ailienant-core

 Esta estructura corresponde al motor principal de LangGraph, la lógica de los agentes de inteligencia artificial y el servidor FastAPI.

```text
 📦ailienant-core
 ┣ 📂agents
 ┃ ┣ 📜planner.py
 ┃ ┗ 📜prompts.py
 ┣ 📂api
 ┃ ┣ 📜api_contracts.py
 ┃ ┣ 📜websocket_manager.py
 ┃ ┗ 📜ws_contracts.py
 ┣ 📂brain
 ┃ ┣ 📜checkpoint.py
 ┃ ┣ 📜engine.py
 ┃ ┣ 📜routing_engine.py
 ┃ ┗ 📜state.py
 ┣ 📂core
 ┃ ┣ 📂__pycache__
 ┃ ┃ ┣ 📜vfs_middleware.cpython-313.pyc
 ┃ ┃ ┗ 📜__init__.cpython-313.pyc
 ┃ ┣ 📜task_service.py
 ┃ ┣ 📜vfs_middleware.py
 ┃ ┗ 📜__init__.py
 ┣ 📂shared
 ┃ ┣ 📜config.py
 ┃ ┣ 📜rbac.py
 ┃ ┗ 📜token_counter.py
 ┣ 📂tools
 ┃ ┗ 📜llm_gateway.py
 ┣ 📂__pycache__
 ┃ ┣ 📜api_contracts.cpython-313.pyc
 ┃ ┣ 📜config.cpython-313.pyc
 ┃ ┣ 📜main.cpython-313.pyc
 ┃ ┣ 📜state.cpython-313.pyc
 ┃ ┣ 📜websocket_manager.cpython-313.pyc
 ┃ ┗ 📜ws_contracts.cpython-313.pyc
 ┣ 📜main.py
 ┗ 📜requirements.txt
 ```