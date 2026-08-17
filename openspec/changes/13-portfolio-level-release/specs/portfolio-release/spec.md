## Purpose

Defines the release-readiness behavior AILIENANT must exhibit before its public launch: containerized deployment, a zero-dependency binary install path, finalized visual documentation, an autonomous multi-agent demo, and the closing end-to-end validation gate.

## ADDED Requirements

### Requirement: Single-Command Containerized Launch
The system SHALL provide a `Dockerfile` and `docker-compose.yml` that launch the full architecture — backend and LanceDB — with a single command, requiring no manual environment setup.

#### Scenario: Fresh machine launch
- **WHEN** a user with only Docker installed runs `docker compose up` at the repo root
- **THEN** the backend and LanceDB come up healthy and the system is reachable without any additional manual configuration step

### Requirement: Zero-Friction Binary Install
The system SHALL be installable without the user having Python, Docker, or Node installed: `ailienant-core` (FastAPI + LanceDB + Tree-sitter) SHALL be compiled into a per-OS binary, and the VS Code extension SHALL unpack and run that binary in the background on install.

#### Scenario: Install on a clean machine
- **WHEN** a user installs the VS Code extension on a machine with no Python, Docker, or Node runtime present
- **THEN** the extension unpacks the per-OS binary and starts the backend in the background without prompting the user to install any additional runtime

#### Scenario: Unsupported platform
- **WHEN** the extension is installed on an OS/architecture combination with no compiled binary available
- **THEN** the extension surfaces a clear, actionable error identifying the unsupported platform rather than failing silently or hanging

### Requirement: Finalized Visual Documentation
The system SHALL ship a `README.md` (and its tracked translations) containing real architecture diagrams that accurately reflect the shipped system.

#### Scenario: Architecture diagram accuracy
- **WHEN** a new reader opens `README.md` to understand the system's architecture
- **THEN** the diagrams shown correspond to the actual components and data flow present in the shipped codebase, not a stale or aspirational design

### Requirement: Autonomous Multi-Agent Demo
The system SHALL provide a recorded demonstration of TestAgent, LogicAgent, and AnalystAgent cooperating unattended to diagnose and resolve a cyclic bug, with no human intervention during the run.

#### Scenario: Unattended cyclic-bug resolution
- **WHEN** the recorded demo scenario is run against a seeded cyclic bug
- **THEN** TestAgent, LogicAgent, and AnalystAgent resolve it end-to-end without any human input during the run, and the recording captures the full unattended sequence

### Requirement: Final Checkpoint Gate
The system SHALL pass a zero-friction-install end-to-end validation before Phase 13 is considered closed, exercising the binary install path and container launch path together.

#### Scenario: Closing gate run
- **WHEN** the Phase 13 checkpoint gate is executed
- **THEN** both the zero-friction binary install path and the single-command container launch path complete successfully end-to-end, and the gate result is recorded before the phase is marked complete
