# 📄 SDD Contract: Fase 1.1 - Entropy Extractor (Payload Builder)
**Status:** DRAFT / IN-REVIEW
**Responsables:** Arquitecto IA & Mentor + Team Member
**Dominio:** VS Code Extension (TypeScript) ↔ Backend (FastAPI/LangGraph)

---

## 🎯 1. OUTCOME (Resultado Esperado)
Un módulo de TypeScript (`EntropyBuilder.ts`) capaz de realizar un snapshot determinista y atómico del estado actual del IDE. Debe capturar no solo el texto, sino el **contexto volátil** (archivos no guardados) y la **jerarquía de importancia** (qué está viendo el usuario ahora mismo).

## 🛠️ 2. SCOPE (Alcance Técnico)
- **Captura de Dirty Buffers:** Lectura de documentos en memoria que no han sido persistidos en disco.
- **LSP Synchronization:** Extracción del `version_id` nativo de VS Code para prevenir colisiones de escritura (Race Conditions).
- **Metadata de Cursor:** Captura de la posición (Línea, Columna) y selección activa para inyectar "Atención Focal" al LogicAgent.
- **Payload Packaging:** Serialización en un esquema compatible con Pydantic definido en `state.py`.

## ⚠️ 3. CONSTRAINTS (Restricciones y Riesgos)
- **Latencia de Bloqueo:** El proceso de extracción no debe exceder los **50ms** en el hilo principal del IDE para evitar "lag" perceptible (Jank). 
- **Big O Complexity:** $O(N + D)$ donde $N$ es el número de archivos abiertos y $D$ es el tamaño de los dirty buffers. Debemos evitar el escaneo recursivo de `node_modules`.
- **Privacidad:** Prohibido extraer contenido de archivos ignorados por `.gitignore` o archivos `.env`.

## 🧠 4. DECISIONS (Decisiones Arquitectónicas)
- **Uso de `vscode.workspace.textDocuments`:** Se prefiere sobre `fs.readFile` para capturar el estado real del buffer del editor, no el del disco.
- **Inyección de `document_version_id`:** Se utilizará para implementar **Optimistic Concurrency Control**. Si el backend intenta sugerir un cambio sobre una versión que ya cambió en el IDE, el commit se rechaza.
- **Protocolo de Transporte:** JSON sobre el WebSocket establecido en la Fase 0.2.

## 📝 5. TASKS (Work Breakdown Structure - Nivel 1.1)
1. **[TS]** Implementar `getEntropyPayload()`:
   - Filtrar `isDirty === true`.
   - Obtener `activeTextEditor` para el contexto de foco.
   - Mapear nombres de archivos a URIs relativas al workspace.
2. **[PY]** Actualizar `AIlienantGraphState`:
   - Añadir campo `vfs_snapshot: Dict[str, str]` para almacenar los buffers sucios.
3. **[TS/PY]** Prueba de Integridad: El hash del contenido enviado desde TS debe coincidir con el hash recibido en Python.

## ✅ 6. CHECKS (Criterios de Aceptación)
- [ ] ¿Se capturan los cambios no guardados en el archivo actual?
- [ ] ¿El payload incluye la línea exacta donde está el cursor?
- [ ] ¿El tamaño del payload es manejable (< 2MB para proyectos medianos)?
- [ ] ¿Se maneja correctamente el caso donde no hay editores abiertos?