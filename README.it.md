<!-- markdownlint-disable MD033 MD041 -->
<div align="center">

<img src="assets/logo.svg" alt="AILIENANT" width="340" />

<h1>AILIENANT</h1>

<p><strong>Il compagno di programmazione IA che pianifica prima di programmare — e gira sulla tua macchina, con i tuoi modelli, alle tue condizioni.</strong></p>

<p>
  <a href="README.md">English</a> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.fr.md">Français</a> ·
  <a href="README.zh.md">中文</a> ·
  <a href="README.hi.md">हिन्दी</a> ·
  <a href="README.ru.md">Русский</a> ·
  <strong>Italiano</strong>
</p>

<p>
  <a href="LICENSE"><img alt="Licenza: AGPL-3.0" src="https://img.shields.io/badge/License-AGPL%20v3-blue.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="TypeScript" src="https://img.shields.io/badge/TypeScript-5.9-3178C6?logo=typescript&logoColor=white">
  <img alt="VS Code" src="https://img.shields.io/badge/VS%20Code-Estensione-007ACC?logo=visualstudiocode&logoColor=white">
  <img alt="Stato" src="https://img.shields.io/badge/stato-sviluppo%20attivo-success">
</p>

</div>

---

## Cos'è AILIENANT?

**AILIENANT è un agente di programmazione autonomo che vive dentro VS Code.** Descrivi ciò che vuoi in linguaggio naturale; AILIENANT scrive un piano vero, esegue le modifiche, lancia il codice in un ambiente isolato, legge i risultati e corregge i propri errori — mostrandoti ogni passo del suo ragionamento.

Ciò che lo distingue dagli assistenti IA più diffusi è **dove viene eseguito e come decide.** AILIENANT è **local-first**: può funzionare interamente sulla tua macchina con modelli aperti (Ollama, LM Studio e altri), ricorrendo al cloud solo quando un compito ne ha davvero bisogno — e te lo dice, in dollari, quando lo fa. Il tuo codice non deve lasciare il tuo portatile, e non sei mai vincolato a un unico fornitore.

> **In una riga:** un ingegnere IA privato, attento ai costi e che pianifica per primo, per il tuo codice — open source e senza lock-in del fornitore.

---

## Perché lo si usa

- **🧠 Pianifica prima di programmare.** Un *Pianificatore* dedicato trasforma la tua richiesta in una specifica concreta e in un elenco di attività, congela l'ambito e sorveglia la "deriva" così che l'agente non si allontani in silenzio riscrivendo metà del progetto. Un *Programmatore* separato esegue quel piano. Due teste, ciascuna che fa bene una cosa.
- **🔒 Il tuo codice resta tuo.** Funziona al 100% in locale con i tuoi modelli. Nessun cloud obbligatorio, nessuna telemetria che "chiama casa", nessun addestramento sul tuo repository.
- **💸 Vedi il costo.** Ogni attività ha un registro dei token in tempo reale e un tetto di budget rigido. L'uso locale rispetto a quello cloud e il risparmio stimato sono mostrati, non nascosti.
- **🪟 Vedi il ragionamento.** Una "Casella dei Pensieri" dal vivo trasmette il ragionamento del modello, e una traccia passo-passo mostra ogni file letto, comando eseguito e patch proposta.
- **⏪ Puoi riavvolgere.** Ogni passo di un'attività è un checkpoint durevole. Ramifica da qualsiasi punto per esplorare un'alternativa — vero debug con viaggio nel tempo per un agente.
- **🛡️ Esegue il codice in sicurezza.** I comandi generati vengono eseguiti in un ambiente isolato (Docker, con fallback WebAssembly e ad approvazione umana), mai alla cieca sulla tua macchina.
- **🔌 Nessun vincolo.** Porta il tuo modello e fornitore — Ollama, LM Studio, vLLM, llama.cpp, OpenAI, Anthropic, Google, DeepSeek, Mistral e altri — e cambialo quando vuoi.

---

## In cosa è diverso?

| | **AILIENANT** | Assistente cloud tipico |
| --- | --- | --- |
| Gira interamente sulla tua macchina | ✅ Local-first, modello proprio | ❌ Solo cloud |
| Pianifica e poi programma (bicefalo) | ✅ Pianificatore + Programmatore, con guardia di deriva | ❌ Un modello, un tentativo |
| Instradamento intelligente locale↔cloud | ✅ Sceglie il livello più economico adatto | ❌ Fisso |
| Mostra il costo in tempo reale | ✅ Registro token + tetto di budget | ⚠️ Spesso nascosto |
| Viaggio nel tempo / ramificare un'esecuzione | ✅ Checkpoint durevoli | ❌ Senza stato |
| Esecuzione isolata | ✅ Docker / Wasm / con approvazione | ⚠️ Spesso sull'host |
| Lock-in del fornitore | ✅ Nessuno — cambia liberamente | ❌ Vincolato a uno |

Un confronto tecnico più completo è in **[HowItWorks.md](HowItWorks.md)**.

---

## Sicurezza e protezione, by design

AILIENANT presume che prima o poi un agente autonomo tenterà di fare qualcosa che non dovrebbe — ed è costruito per contenerlo.

- **Isolato di default.** I comandi vengono eseguiti in un container Docker isolato (workspace di sola lettura, senza rete, non-root) con fallback WebAssembly e intervento umano quando Docker non è disponibile.
- **Permessi fail-closed.** Ogni strumento è classificato per privilegio; tutto ciò che non è riconosciuto è trattato come **pericoloso fino a prova contraria**, mai il contrario.
- **Approvazione umana dove conta.** Le azioni rischiose e gli sforamenti di budget si fermano per la tua approvazione esplicita.
- **Traccia di audit a prova di manomissione.** Le approvazioni sono registrate in un libro mastro concatenato crittograficamente (blake2b) che puoi verificare.
- **Isolamento multi-tenant.** Ogni frammento di memoria indicizzata è legato al suo workspace, così i progetti non si mescolano mai tra loro.

---

## Avvio rapido

> Guida completa: **[HowToUseIt.md](HowToUseIt.md)**

**Prerequisiti:** Python 3.10+ (3.13 consigliato), Node.js 20+, VS Code 1.85+ e almeno una sorgente di modelli (un'installazione locale Ollama/LM Studio, un proxy [LiteLLM](https://docs.litellm.ai/docs/simple_proxy), o chiavi API cloud).

```powershell
# 1. Backend (il motore di orchestrazione)
cd ailienant-core
python -m venv venv
.\venv\Scripts\activate          # Unix: source venv/bin/activate
pip install -r requirements.txt
copy ..\.env.example ..\.env     # Unix: cp ../.env.example ../.env

# 2. Estensione (l'interfaccia VS Code)
cd ..\ailienant-extension
npm install
npm run compile
```

Poi apri il progetto in VS Code e premi **F5** per avviare l'estensione. La prima volta che apri una sessione AILIENANT, avvierà il backend per te e inizierà a indicizzare il tuo workspace. Configura i tuoi modelli dal pannello **BYOM** integrato, digita una richiesta e sei pronto.

---

## Come funziona (versione breve)

```
Chiedi  ─▶  Pianificatore ─▶ guardia di ─▶ Programmatore ─▶ il sandbox lo esegue
            (scrive spec      deriva        (modifica         ▲      │
             + piano)         (ambito        file)            │      ▼
                              bloccato)                  correggi ◀─ leggi il risultato
```

Dietro le quinte, un motore **LangGraph** con stato instrada ogni attività tra modelli locali e cloud usando un punteggio di contesto e complessità, recupera i file giusti con **GraphRAG** (ricerca vettoriale + una visita delle dipendenze a un salto) e salva un checkpoint a ogni passo per non perdere nulla. La versione approfondita — diagrammi, la matematica dell'instradamento, il ciclo di esecuzione e il modello di sicurezza — è in **[HowItWorks.md](HowItWorks.md)**.

---

## Documentazione

| Documento | Per chi |
| --- | --- |
| **[HowToUseIt.md](HowToUseIt.md)** | Chiunque — installare, configurare ed eseguire la prima attività, passo dopo passo |
| **[HowItWorks.md](HowItWorks.md)** | I curiosi — architettura, instradamento e modello di sicurezza spiegati |
| **[DEVELOPERS.md](DEVELOPERS.md)** | Sviluppatori del core — interni approfonditi, diagrammi, pseudocodice, mappa del codice |
| **[CONTRIBUTING.md](CONTRIBUTING.md)** | Collaboratori — setup, standard e come inviare una buona PR |
| **[docs/PROJECT_MANIFEST.md](docs/PROJECT_MANIFEST.md)** | La roadmap completa, fase per fase |

---

## Contribuire

AILIENANT è open source e i contributi sono benvenuti — dal correggere un refuso al chiudere un obiettivo della roadmap. Inizia da **[CONTRIBUTING.md](CONTRIBUTING.md)**.

Una cosa da sapere fin da subito: poiché il progetto è a doppia licenza (vedi sotto), ogni collaboratore firma un breve **[Accordo di Licenza per Contributori (CLA)](CLA.md)** prima che la sua prima PR venga unita. È un passaggio unico e mantieni il copyright sul tuo lavoro.

---

## Licenza

AILIENANT è **open-core e a doppia licenza**:

- **Edizione Community — [GNU AGPL-3.0](LICENSE).** Libera di usare, studiare, modificare e condividere. Se la distribuisci o esegui una versione modificata come servizio di rete, condividi il tuo codice sorgente sotto la stessa licenza.
- **Edizione Commerciale / Enterprise.** Per le organizzazioni che non possono accettare i termini dell'AGPL o che vogliono funzionalità e supporto enterprise.

Vedi **[LICENSING.md](LICENSING.md)** per il quadro completo e come ottenere una licenza commerciale.

> Il nome **AILIENANT** e i suoi loghi sono marchi del progetto e non sono coperti dall'AGPL.

---

<div align="center">

**Costruito per ingegneri che vogliono un compagno IA di cui potersi davvero fidare — e che possono verificare.**

Sulle spalle di <a href="https://github.com/langchain-ai/langgraph">LangGraph</a> · <a href="https://lancedb.com/">LanceDB</a> · <a href="https://tree-sitter.github.io/">Tree-sitter</a> · <a href="https://github.com/BerriAI/litellm">LiteLLM</a> · <a href="https://docs.pydantic.dev/">Pydantic</a>.

</div>
