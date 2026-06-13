<!-- markdownlint-disable MD033 MD041 -->
<div align="center">

<img src="assets/icon-color.svg" alt="AILIENANT" width="340" />

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

- **🧠 Pianifica prima di programmare.** Un vero team di agenti specializzati — un *Ricercatore* mappa il tuo codice, un *Pianificatore* trasforma la richiesta in una specifica concreta e in un elenco di attività e congela l'ambito, un *Orchestratore* guida i passi, un *Programmatore* (in uno dei suoi 8 ruoli esperti) esegue le modifiche, e un *Analista* con cui puoi conversare spiega il codice. Una guardia di deriva impedisce all'agente di allontanarsi in silenzio riscrivendo metà del progetto.
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
| Indaga, pianifica, programma e si autoverifica | ✅ Un team di 5 agenti con guardia di deriva | ❌ Un modello, un tentativo |
| Instradamento intelligente locale↔cloud | ✅ Sceglie il livello più economico adatto | ❌ Fisso |
| Mostra il costo in tempo reale | ✅ Registro token + tetto di budget | ⚠️ Spesso nascosto |
| Viaggio nel tempo / ramificare un'esecuzione | ✅ Checkpoint durevoli | ❌ Senza stato |
| Esecuzione isolata | ✅ Docker / Wasm / con approvazione | ⚠️ Spesso sull'host |
| Lock-in del fornitore | ✅ Nessuno — cambia liberamente | ❌ Vincolato a uno |

Un confronto tecnico più completo è in **[HowItWorks.md](HowItWorks.md)**.

---

## Il team interno

AILIENANT non è un singolo modello che fa tutto — è una piccola squadra di specialisti, ciascuno con un compito, collegati da un motore **LangGraph** con stato:

| Agente | Cosa fa |
| --- | --- |
| 🔭 **Ricercatore** | Costruisce una "mappa scheletro" del tuo codice — firme e relazioni tra moduli — così che il Pianificatore ragioni sulla struttura reale, non su congetture. |
| 🧭 **Pianificatore** | Trasforma la tua richiesta in una specifica concreta e validata e in un elenco di attività (una WBS), poi **congela l'ambito** così che il lavoro non dilaghi. |
| 🎛️ **Orchestratore** | Guida il piano passo dopo passo, coordinando lo stato e instradando ogni passo al livello di modello giusto. |
| 🛠️ **Programmatore** | Esegue le modifiche vere — adottando uno dei suoi **8 ruoli esperti** per ogni attività. |
| 💬 **Analista (Natt)** | Un tutor di sola lettura con cui conversare. Spiega il tuo codice e AILIENANT stesso, ma non tocca mai i file — la *voce*, non la *mano*. |

Il Programmatore si specializza nel ruolo richiesto da ogni attività: **core-dev, architetto/refactor, devops/infra, secops, qa-tester, doc-manager, vcs-manager, ingegnere dati/ML** — ciascuno con i propri strumenti, protezioni e trigger di approvazione (es. una modifica a `.env` si ferma sempre per te).

Quando un passo fallisce, un ciclo di **auto-riparazione** legge l'errore e propone una patch correttiva prima di arrendersi; per i passi aperti, una **cella ReAct** limitata lavora su un terminale dal vivo fino a portare a termine il compito. Il dettaglio completo per agente è in **[HowItWorks.md](HowItWorks.md)**.

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

Poi apri il progetto in VS Code e premi **F5** per avviare l'estensione. La prima volta che apri una sessione AILIENANT, **avvia il backend per te su una porta locale assegnata automaticamente** (una porta `127.0.0.1` libera, es. `http://127.0.0.1:59247/`) e vi collega l'interfaccia — non c'è alcuna porta da configurare. Poi inizia a indicizzare il tuo workspace. Configura i tuoi modelli dal pannello **BYOM** integrato, digita una richiesta e sei pronto.

> Avvii il backend a mano (headless / CI)? Lancialo con `uvicorn main:app --port 8000` e punta l'impostazione `backendUrl` dell'estensione su di esso. La porta assegnata automaticamente serve solo per il flusso normale dentro VS Code.

---

## Come funziona (versione breve)

```
Chiedi ─▶ Ricercatore ─▶ Pianificatore ─▶ guardia di ─▶ Programmatore ─▶ il sandbox lo esegue
          (mappa il       (spec +          deriva        (modifica           ▲      │
           codice)         piano)           (blocco)      file)               │      ▼
                                                              auto-riparazione ◀─ legge il risultato
```

Dietro le quinte, un motore **LangGraph** con stato instrada ogni attività tra modelli locali e cloud usando un punteggio di contesto e complessità — scegliendo sempre il **livello più economico in grado di svolgere il lavoro** e ricorrendo al cloud solo quando un compito ne ha davvero bisogno.

Recupera i file giusti con **GraphRAG**: invece di riversare interi file nel prompt, indicizza il tuo codice come un grafo di dipendenze (Tree-sitter) con embedding vettoriali, poi estrae solo la fetta rilevante tramite ricerca vettoriale + una visita delle dipendenze a k salti ordinata per importanza (PageRank). Questo mantiene i prompt piccoli — una **riduzione media di circa il 70% della dimensione del prompt** — ed è proprio ciò che permette ad AILIENANT di **girare bene su hardware modesto**: i budget per livello tengono il contesto entro la finestra di un piccolo modello locale (anche solo 4K token), e l'indice risiede in un archivio veloce, in RAM. Ogni passo ha un checkpoint per non perdere nulla.

**Costruito su una specifica, non su supposizioni.** Prima di toccare qualsiasi file, il Pianificatore trasforma la tua richiesta in una `MissionSpecification` congelata — risultato atteso, scope, passi WBS, vincoli e criteri di accettazione (terminologia TDD e DDD inclusa). Una volta congelata, né il Pianificatore né il Programmatore possono modificare silenziosamente lo scope: un `drift_monitor` confronta ogni ripianificazione con l'originale usando una metrica di similarità multi-fattore e ti coinvolge se rileva una deviazione. La specifica è il contratto; l'agente non può auto-autorizzarsi modifiche allo scope.

**I fallimenti vengono instradati, non causano crash.** Ogni turno dell'agente viene eseguito all'interno di un harness di esecuzione strutturato: un `reflexion_guard` intercetta le eccezioni e le instrada verso un agente di riparazione dedicato (invece di mostrare un traceback), un `finops_gate` deterministico applica il tuo limite di costo ad ogni passo del grafo, e verdetti strutturati — non stdout grezzo — guidano tutte le decisioni di retry. Se un nodo ha un'eccezione non gestita, viene scritta in una coda dead-letter prima che l'errore si propaghi, così puoi ispezionare e riprendere.

La versione approfondita — diagrammi, lo schema completo della specifica e la logica di instradamento della riparazione — è in **[HowItWorks.md](HowItWorks.md)**.

---

## Conversa con il tuo codice: l'Analista

Non ogni domanda richiede che l'agente *faccia* qualcosa — a volte vuoi solo capire. L'**Analista (Natt)** è un compagno di chat in un pannello laterale: chiedigli *«come scorre l'autenticazione in questo servizio?»*, *«cosa si romperebbe se cambiassi questa funzione?»* o persino *«come funziona davvero l'instradamento di AILIENANT?»* e risponde in linguaggio chiaro.

È un **tutor di sola lettura — la voce, mai la mano.** Spiega, traccia e insegna, ma non modifica mai i tuoi file, così puoi esplorare liberamente senza che cambi nulla.

A rendere affidabili le sue risposte è **ciò su cui si fonda** — tre fonti insieme: il **grafo di conoscenza** del tuo codice (per citare la struttura reale, non un'allucinazione), il **README del tuo workspace** (per conoscere l'intento del progetto) e la **documentazione di prodotto di AILIENANT** stessa (per spiegare lo strumento). E poiché spiegare costa meno che programmare, **scegli il modello di risposta** da un piccolo selettore — un modello locale veloce per le domande rapide, uno più potente per una panoramica architetturale approfondita — senza intaccare la qualità del recupero.

---

## Una memoria che puoi vedere

La comprensione che AILIENANT ha del tuo codice non è una scatola nera. Il **pannello di controllo** integrato rappresenta l'indice GraphRAG come un **grafo di conoscenza interattivo** — una mappa a forze dei tuoi file e delle loro dipendenze, dove i file "hub" più connessi spiccano, i moduli affini condividono un colore e l'importanza (PageRank) guida la disposizione. Una **mappa vettoriale** 2D di accompagnamento proietta come il motore raggruppa il tuo codice *semanticamente*. È un'immagine viva di ciò che l'agente sa, e di come decide cosa leggere.

---

## Un ecosistema aperto

- **🧩 Server MCP.** AILIENANT parla il **Model Context Protocol**, con un registro curato di server verificati (GitHub, Brave Search, Docker, Postgres) attivabili con un clic. Ogni strumento MCP è **classificato per privilegio** — gli sconosciuti sono trattati come pericolosi fino a prova contraria — e considerato attendibile solo per la sessione dopo la tua approvazione.
- **⚡ Skills.** Salva snippet di istruzioni riutilizzabili — globali o per workspace — e inseriscili in qualsiasi prompt. I tuoi modelli di comando, versionati con il progetto.
- **🧰 Strumenti.** Gli agenti agiscono tramite un registro di strumenti tipizzato e regolato per ruolo: leggere e tracciare il codice, modificare file in modo transazionale, eseguire comandi nel sandbox e chiederti quando sono incerti. Il catalogo sta **crescendo verso ~56 strumenti assegnati per ruolo** (vedi la roadmap in **[docs/PROJECT_MANIFEST.md](docs/PROJECT_MANIFEST.md)**); la tabella completa — quale agente usa quale strumento — è in **[HowItWorks.md](HowItWorks.md)**.

---

## Dreaming: migliora mentre sei via

Programmare è a raffiche — esci a pranzo, ti disconnetti per la notte. La **Modalità Dreaming** trasforma quel tempo inattivo in progresso. Indichi ad AILIENANT a cosa pensare — *architettura e pattern*, *refactoring e debito tecnico*, *correzioni di bug*, l'intero workspace, o un tema che digiti — e mentre sei via lavora quel focus in autonomia: studiando il codice, **consolidando ciò che apprende nella memoria a lungo termine** ed esplorando miglioramenti. Si auto-corregge strada facendo e **si ferma da solo se gli errori iniziano ad accumularsi**.

Soprattutto, **non si sveglia mai con un timer per invadere la tua macchina** — sei *tu* a decidere quando spendere le risorse, avviandolo quando ti allontani. È **limitato dal budget** (rifiuta una volta raggiunto il tetto di spesa della sessione) ed è sicuro: se torni e salvi un file a metà di una passata, quella passata si interrompe in modo pulito senza scrivere.

Scegli il **profilo** adatto alla pausa che ti stai prendendo — bilanciano velocità, costo e profondità:

| Profilo | Ideale per | Circa |
| --- | --- | --- |
| **Medium** | Una pausa pranzo — leggero, interamente locale | 1 attività · 3 file · ~60 min |
| **Big** | Tutta la notte — più profondo, più file, locale | 3 attività · 10 file · notturno |
| **Cloud** | Ragionamento di altissima qualità, limitato dai token | 1 attività · 5 file · con tetto di token |
| **Hybrid** | Il cloud *pianifica*, il modello locale *modifica* — qualità a costo inferiore | 2 attività · 6 file |

Il meccanismo completo — cosa può davvero ottenere ogni profilo, le finestre temporali e come la ricerca ad albero offline (MCTS) valida le modifiche candidate — è in **[HowItWorks.md](HowItWorks.md)**.

---

## Terminale dal vivo e pannello di controllo

L'agente lavora su un **terminale persistente e interattivo** — una vera sessione shell che ricorda la sua directory di lavoro e l'ambiente tra i comandi, trasmette l'output dal vivo e può essere interrotta — il tutto dentro il sandbox. Il **pannello di controllo** (una dashboard integrata, servita localmente) ti offre undici viste su una sessione in corso: telemetria di costo e instradamento, stato di hardware e runtime, il grafo della memoria, i modelli BYOM, i server MCP e le skills, le regole di governance, un'area di staging per rivedere le patch in sospeso, un libro mastro di audit a prova di manomissione e il recupero dopo un crash.

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
