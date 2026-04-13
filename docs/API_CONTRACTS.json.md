 ## API_CONTRACTS.json

Este documento define cómo la extensión de VS Code se comunica con el motor de Ialienant (FastAPI).

```json
{
  "_meta": {
    "version": "1.0.0",
    "description": "Contratos API entre VS Code Client (Frontend) y FastAPI Orchestrator (Backend)"
  },
  "endpoints": {
    "POST /api/v1/task/submit": {
      "description": "Envía un nuevo prompt del usuario al sistema (Modo Auto o Manual).",
      "request": {
        "user_input": "string",
        "manual_agent": "string | null",
        "ide_context": {
          "active_file": "string",
          "cursor_position": "integer",
          "selected_text": "string"
        }
      },
      "response": {
        "task_id": "string",
        "status": "QUEUED"
      }
    },
    "WS /ws/v1/stream/{task_id}": {
      "description": "WebSocket bidireccional para streaming de tokens, actualización del grafo y CSS.",
      "messages_server_to_client": [
        {
          "type": "TOKEN_CHUNK",
          "payload": { "agent": "LogicAgent", "text": "def init_auth():\n" }
        },
        {
          "type": "TELEMETRY_UPDATE",
          "payload": { "css_score": 82.5, "routing": "LOCAL_SMALL", "latency_ms": 120 }
        },
        {
          "type": "GRAPH_MUTATION",
          "payload": { "node_updated": "auth_module.py", "status": "STABLE" }
        },
        {
          "type": "HITL_APPROVAL_REQUIRED",
          "payload": { "command": "npm install pydantic", "reason": "Modificación de entorno" }
        }
      ],
      "messages_client_to_server": [
        {
          "type": "HITL_RESPONSE",
          "payload": { "approved": true }
        }
      ]
    },
    "POST /api/v1/memory/reindex": {
      "description": "Fuerza una sincronización entre el workspace de VS Code, LanceDB y NetworkX.",
      "request": {
        "target_paths": ["string"]
      },
      "response": {
        "status": "SUCCESS",
        "files_indexed": "integer",
        "drifts_resolved": "integer"
      }
    }
  }
}