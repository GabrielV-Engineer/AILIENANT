<!-- markdownlint-disable MD033 MD041 -->
<div align="center">

<img src="assets/icon-color.svg" alt="AILIENANT" width="340" />

<h1>AILIENANT</h1>

<p><strong>Le coéquipier de programmation IA qui planifie avant de coder — et qui tourne sur votre machine, avec vos modèles, selon vos règles.</strong></p>

<p>
  <a href="README.md">English</a> ·
  <a href="README.es.md">Español</a> ·
  <strong>Français</strong> ·
  <a href="README.zh.md">中文</a> ·
  <a href="README.hi.md">हिन्दी</a> ·
  <a href="README.ru.md">Русский</a> ·
  <a href="README.it.md">Italiano</a>
</p>

<p>
  <a href="LICENSE"><img alt="Licence : AGPL-3.0" src="https://img.shields.io/badge/License-AGPL%20v3-blue.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="TypeScript" src="https://img.shields.io/badge/TypeScript-5.9-3178C6?logo=typescript&logoColor=white">
  <img alt="VS Code" src="https://img.shields.io/badge/VS%20Code-Extension-007ACC?logo=visualstudiocode&logoColor=white">
  <img alt="Statut" src="https://img.shields.io/badge/statut-d%C3%A9veloppement%20actif-success">
</p>

</div>

---

## Qu'est-ce qu'AILIENANT ?

**AILIENANT est un agent de programmation autonome qui vit dans VS Code.** Vous décrivez ce que vous voulez en langage naturel ; AILIENANT rédige un véritable plan, effectue les modifications, exécute le code dans un bac à sable, lit les résultats et corrige ses propres erreurs — tout en vous montrant chaque étape de son raisonnement.

Ce qui le distingue des assistants IA populaires, c'est **où il s'exécute et comment il décide.** AILIENANT est **local-first** : il peut fonctionner entièrement sur votre propre machine avec des modèles ouverts (Ollama, LM Studio et d'autres), ne sollicitant le cloud que lorsqu'une tâche en a réellement besoin — et il vous le signale, en dollars, le cas échéant. Votre code n'a pas à quitter votre ordinateur, et vous n'êtes jamais enfermé chez un seul fournisseur.

> **En une phrase :** un ingénieur IA privé, soucieux des coûts et qui planifie d'abord, pour votre base de code — open source et sans dépendance à un fournisseur.

---

## Pourquoi on l'utilise

- **🧠 Il planifie avant de coder.** Une véritable équipe d'agents spécialisés — un *Chercheur* cartographie votre code, un *Planificateur* transforme la demande en une spécification concrète et une liste de tâches et fige le périmètre, un *Orchestrateur* pilote les étapes, un *Codeur* (dans l'un de ses 8 rôles experts) effectue les modifications, et un *Analyste* avec qui vous pouvez discuter explique la base de code. Une garde de dérive empêche l'agent de s'égarer en silence et de réécrire la moitié de votre projet.
- **🔒 Votre code reste le vôtre.** Fonctionne à 100 % en local avec vos propres modèles. Pas de cloud obligatoire, pas de télémétrie qui « appelle la maison », pas d'entraînement sur votre dépôt.
- **💸 Vous voyez le coût.** Chaque tâche possède un registre de jetons en temps réel et un plafond budgétaire strict. L'usage local vs. cloud et les économies estimées sont affichés, pas cachés.
- **🪟 Vous voyez le raisonnement.** Une « Boîte de pensée » en direct diffuse le raisonnement du modèle, et une trace étape par étape montre chaque fichier lu, commande exécutée et correctif proposé.
- **⏪ Vous pouvez rembobiner.** Chaque étape d'une tâche est un point de contrôle durable. Bifurquez depuis n'importe quel point pour explorer une alternative — un véritable débogage par voyage dans le temps pour un agent.
- **🛡️ Il exécute le code en sécurité.** Les commandes générées s'exécutent dans un bac à sable (Docker, avec des solutions de repli WebAssembly et validation humaine), jamais à l'aveugle contre votre machine.
- **🔌 Aucun verrouillage.** Apportez votre propre modèle et fournisseur — Ollama, LM Studio, vLLM, llama.cpp, OpenAI, Anthropic, Google, DeepSeek, Mistral et plus — et changez quand vous voulez.

---

## En quoi est-il différent ?

| | **AILIENANT** | Assistant cloud classique |
| --- | --- | --- |
| Tourne entièrement sur votre machine | ✅ Local-first, modèle perso | ❌ Cloud uniquement |
| Cherche, planifie, code et se vérifie | ✅ Une équipe de 5 agents avec garde de dérive | ❌ Un modèle, un essai |
| Routage intelligent local↔cloud | ✅ Choisit le niveau le moins cher qui convient | ❌ Fixe |
| Affiche le coût en temps réel | ✅ Registre de jetons + plafond budgétaire | ⚠️ Souvent caché |
| Voyage dans le temps / bifurquer une exécution | ✅ Points de contrôle durables | ❌ Sans état |
| Exécution en bac à sable | ✅ Docker / Wasm / soumise à validation | ⚠️ Souvent sur l'hôte |
| Verrouillage fournisseur | ✅ Aucun — changez librement | ❌ Lié à un seul |

Une comparaison technique plus complète se trouve dans **[HowItWorks.md](HowItWorks.md)**.

---

## L'équipe à l'intérieur

AILIENANT n'est pas un seul modèle qui fait tout — c'est une petite équipe de spécialistes, chacun avec une tâche, reliés par un moteur **LangGraph** à états :

| Agent | Ce qu'il fait |
| --- | --- |
| 🔭 **Chercheur** | Construit une « carte squelette » de votre code — signatures et relations entre modules — pour que le Planificateur raisonne sur la structure réelle, pas sur des suppositions. |
| 🧭 **Planificateur** | Transforme votre demande en une spécification concrète et validée et une liste de tâches (une WBS), puis **fige le périmètre** pour que le travail ne déborde pas. |
| 🎛️ **Orchestrateur** | Pilote le plan étape par étape, coordonnant l'état et routant chaque étape vers le bon niveau de modèle. |
| 🛠️ **Codeur** | Effectue les modifications réelles — en adoptant l'un de ses **8 rôles experts** par tâche. |
| 💬 **Analyste (Natt)** | Un tuteur en lecture seule avec qui discuter. Il explique votre code et AILIENANT lui-même, mais ne touche jamais aux fichiers — la *voix*, pas la *main*. |

Le Codeur se spécialise dans le rôle dont chaque tâche a besoin : **core-dev, architecte/refactor, devops/infra, secops, qa-tester, doc-manager, vcs-manager, ingénieur data/ML** — chacun avec ses propres outils, garde-fous et déclencheurs d'approbation (p. ex. une modification de `.env` s'interrompt toujours pour vous).

Quand une étape échoue, une boucle d'**auto-réparation** lit l'erreur et propose un correctif avant d'abandonner ; pour les étapes ouvertes, une **cellule ReAct** bornée travaille contre un terminal en direct jusqu'à l'aboutissement. Le détail complet par agent est dans **[HowItWorks.md](HowItWorks.md)**.

---

## Sécurité et sûreté, par conception

AILIENANT part du principe qu'un agent autonome finira par tenter quelque chose qu'il ne devrait pas — et il est conçu pour le contenir.

- **Isolé par défaut.** Les commandes s'exécutent dans un conteneur Docker isolé (workspace en lecture seule, sans réseau, non-root) avec des solutions de repli WebAssembly et une intervention humaine quand Docker n'est pas disponible.
- **Permissions fail-closed.** Chaque outil est classé par privilège ; tout ce qui n'est pas reconnu est traité comme **dangereux jusqu'à preuve du contraire**, jamais l'inverse.
- **Validation humaine là où ça compte.** Les actions risquées et les dépassements de budget s'interrompent pour votre approbation explicite.
- **Journal d'audit inviolable.** Les approbations sont consignées dans un registre chaîné cryptographiquement (blake2b) que vous pouvez vérifier.
- **Isolation multi-locataire.** Chaque fragment de mémoire indexée est rattaché à son workspace, de sorte que les projets ne fuient jamais entre eux.

---

## Démarrage rapide

> Guide complet : **[HowToUseIt.md](HowToUseIt.md)**

**Prérequis :** Python 3.10+ (3.13 recommandé), Node.js 20+, VS Code 1.85+ et au moins une source de modèles (une installation locale Ollama/LM Studio, un proxy [LiteLLM](https://docs.litellm.ai/docs/simple_proxy), ou des clés d'API cloud).

```powershell
# 1. Backend (le moteur d'orchestration)
cd ailienant-core
python -m venv venv
.\venv\Scripts\activate          # Unix : source venv/bin/activate
pip install -r requirements.txt
copy ..\.env.example ..\.env     # Unix : cp ../.env.example ../.env

# 2. Extension (l'interface VS Code)
cd ..\ailienant-extension
npm install
npm run compile
```

Ouvrez ensuite le projet dans VS Code et appuyez sur **F5** pour lancer l'extension. À la première ouverture d'une session AILIENANT, elle **démarre le backend pour vous sur un port local attribué automatiquement** (un port `127.0.0.1` libre, p. ex. `http://127.0.0.1:59247/`) et y relie l'interface — aucun port à configurer. Elle commence ensuite à indexer votre workspace. Configurez vos modèles depuis le panneau **BYOM** intégré, saisissez une demande, et c'est parti.

> Vous lancez le backend à la main (sans interface / CI) ? Démarrez-le avec `uvicorn main:app --port 8000` et pointez le réglage `backendUrl` de l'extension vers lui. Le port attribué automatiquement ne concerne que le flux normal dans VS Code.

---

## Comment ça marche (version courte)

```
Vous demandez ─▶ Chercheur ─▶ Planificateur ─▶ garde de ─▶ Codeur ─▶ le bac à sable l'exécute
                 (cartographie  (spec +          dérive       (édite        ▲      │
                  le code)       plan)            (verrou)      fichiers)      │      ▼
                                                                    auto-réparation ◀─ lit le résultat
```

En coulisses, un moteur **LangGraph** à états route chaque tâche entre modèles locaux et cloud à l'aide d'un score de contexte et de complexité — choisissant toujours le **niveau le moins cher capable de faire le travail** et ne sollicitant le cloud que lorsqu'une tâche en a vraiment besoin.

Il récupère les bons fichiers avec **GraphRAG** : au lieu de déverser des fichiers entiers dans le prompt, il indexe votre code comme un graphe de dépendances (Tree-sitter) avec des embeddings vectoriels, puis n'extrait que la tranche pertinente via une recherche vectorielle + un parcours de dépendances à k sauts ordonné par importance (PageRank). Cela garde les prompts petits — une **réduction moyenne d'environ 70 % de la taille du prompt** — ce qui est précisément ce qui permet à AILIENANT de **bien tourner sur du matériel modeste** : les budgets par niveau maintiennent le contexte dans la fenêtre d'un petit modèle local (aussi peu que 4 K jetons), et l'index réside dans un magasin rapide, en RAM. Chaque étape a un point de contrôle pour ne rien perdre.

**Construit sur une spécification, pas sur des suppositions.** Avant de toucher un seul fichier, le Planificateur transforme votre requête en une `MissionSpecification` gelée — résultat attendu, périmètre, étapes WBS, contraintes et critères d'acceptation (terminologie TDD et DDD incluse). Une fois gelée, ni le Planificateur ni le Codeur ne peuvent modifier silencieusement le périmètre : un `drift_monitor` compare chaque replanification à l'original via une métrique de similarité multi-facteurs et vous consulte en cas de dérive. La spécification est le contrat ; l'agent ne peut pas s'autoriser lui-même des changements de périmètre.

**Les échecs se routent, ils ne provoquent pas de crash.** Chaque tour d'agent s'exécute dans un harness d'exécution structuré : un `reflexion_guard` intercepte les exceptions et les route vers un agent de réparation dédié (au lieu d'afficher une trace), un `finops_gate` déterministe applique votre plafond de coût à chaque étape du graphe, et des verdicts structurés — pas de stdout brut — pilotent toutes les décisions de retry. Si un nœud a une exception non gérée, elle est écrite dans une file de lettres mortes avant que l'erreur se propage, vous permettant d'inspecter et de reprendre.

La version approfondie — diagrammes, schéma complet de la spécification et logique de routage de réparation — est dans **[HowItWorks.md](HowItWorks.md)**.

---

## Discutez avec votre code : l'Analyste

Toute question n'exige pas que l'agent *fasse* quelque chose — parfois vous voulez juste comprendre. L'**Analyste (Natt)** est un compagnon de discussion dans un panneau latéral : demandez-lui *« comment l'authentification circule-t-elle dans ce service ? »*, *« qu'est-ce qui casserait si je modifiais cette fonction ? »* ou même *« comment fonctionne réellement le routage d'AILIENANT ? »* et il répond en langage clair.

C'est un **tuteur en lecture seule — la voix, jamais la main.** Il explique, trace et enseigne, mais ne modifie jamais vos fichiers, vous pouvez donc explorer librement sans qu'il ne change quoi que ce soit.

Ce qui rend ses réponses dignes de confiance, c'est **ce sur quoi il s'appuie** — trois sources à la fois : le **graphe de connaissances** de votre code (pour citer la structure réelle, pas une hallucination), le **README de votre workspace** (pour connaître l'intention de votre projet) et la **documentation produit d'AILIENANT** elle-même (pour expliquer l'outil). Et comme expliquer coûte moins cher que coder, vous **choisissez le modèle de réponse** depuis un petit sélecteur — un modèle local rapide pour les questions rapides, un plus puissant pour une visite architecturale approfondie — sans affecter la qualité de la récupération.

---

## Une mémoire que vous pouvez voir

La compréhension qu'AILIENANT a de votre code n'est pas une boîte noire. Le **tableau de bord** intégré représente l'index GraphRAG comme un **graphe de connaissances interactif** — une carte dirigée par forces de vos fichiers et de leurs dépendances, où les fichiers « concentrateurs » les plus connectés se démarquent, les modules apparentés partagent une couleur et l'importance (PageRank) guide la disposition. Une **carte vectorielle** 2D associée projette la façon dont le moteur regroupe votre code *sémantiquement*. C'est une image vivante de ce que l'agent sait, et de la manière dont il décide quoi lire.

---

## Un écosystème ouvert

- **🧩 Serveurs MCP.** AILIENANT parle le **Model Context Protocol**, avec un registre curé de serveurs vérifiés (GitHub, Brave Search, Docker, Postgres) activables en un clic. Chaque outil MCP est **classé par privilège** — les inconnus sont traités comme dangereux jusqu'à preuve du contraire — et n'est approuvé que pour la session après votre validation.
- **⚡ Skills.** Enregistrez des extraits d'instructions réutilisables — globaux ou par workspace — et insérez-les dans n'importe quel prompt. Vos propres modèles de commandes, versionnés avec le projet.
- **🧰 Outils.** Les agents agissent via un registre d'outils typé et restreint par rôle : lire et tracer le code, éditer des fichiers de façon transactionnelle, exécuter des commandes dans le bac à sable et vous interroger en cas de doute. Le catalogue **grandit vers ~56 outils assignés par rôle** (voir la feuille de route dans **[docs/PROJECT_MANIFEST.md](docs/PROJECT_MANIFEST.md)**) ; la table complète — quel agent utilise quel outil — est dans **[HowItWorks.md](HowItWorks.md)**.

---

## Dreaming : il s'améliore pendant votre absence

Coder se fait par à-coups — vous sortez déjeuner, vous vous déconnectez le soir. Le **Mode Dreaming** transforme ce temps d'inactivité en progrès. Vous indiquez à AILIENANT à quoi réfléchir — *architecture et motifs*, *refactorisation et dette technique*, *corrections de bugs*, l'ensemble du workspace, ou un thème que vous saisissez — et pendant votre absence il travaille ce focus de façon autonome : étudiant le code, **consolidant ce qu'il apprend dans la mémoire à long terme** et explorant des améliorations. Il s'auto-corrige en chemin et **s'arrête de lui-même si les erreurs commencent à s'accumuler**.

Surtout, il **ne se réveille jamais sur une minuterie pour envahir votre machine** — *vous* décidez quand dépenser les ressources en le lançant lorsque vous vous éloignez. Il est **plafonné en budget** (il refuse une fois le plafond de dépense de la session atteint) et sûr : si vous revenez et enregistrez un fichier en cours de passe, cette passe s'interrompt proprement sans rien écrire.

Choisissez le **profil** adapté à la pause que vous prenez — ils arbitrent vitesse, coût et profondeur :

| Profil | Idéal pour | Environ |
| --- | --- | --- |
| **Medium** | Une pause déjeuner — léger, entièrement local | 1 tâche · 3 fichiers · ~60 min |
| **Big** | La nuit — plus profond, plus de fichiers, local | 3 tâches · 10 fichiers · nocturne |
| **Cloud** | Raisonnement de top qualité, borné par les jetons | 1 tâche · 5 fichiers · plafonné en jetons |
| **Hybrid** | Le cloud *planifie*, le modèle local *édite* — qualité à moindre coût | 2 tâches · 6 fichiers |

Le mécanisme complet — ce que chaque profil peut réellement accomplir, les enveloppes de temps et la façon dont la recherche arborescente hors ligne (MCTS) valide les changements candidats — est dans **[HowItWorks.md](HowItWorks.md)**.

---

## Terminal en direct et panneau de contrôle

L'agent travaille contre un **terminal persistant et interactif** — une véritable session shell qui mémorise son répertoire de travail et son environnement entre les commandes, diffuse la sortie en direct et peut être interrompue — le tout dans le bac à sable. Le **panneau de contrôle** (un tableau de bord intégré, servi localement) vous offre onze vues sur une session en cours : télémétrie de coût et de routage, état du matériel et du runtime, le graphe de mémoire, les modèles BYOM, les serveurs MCP et skills, les règles de gouvernance, une zone de staging pour examiner les correctifs en attente, un journal d'audit inviolable et la récupération après panne.

---

## Documentation

| Document | Pour qui |
| --- | --- |
| **[HowToUseIt.md](HowToUseIt.md)** | Tout le monde — installer, configurer et exécuter votre première tâche, pas à pas |
| **[HowItWorks.md](HowItWorks.md)** | Les curieux — architecture, routage et modèle de sécurité expliqués |
| **[DEVELOPERS.md](DEVELOPERS.md)** | Développeurs du cœur — internes détaillés, diagrammes, pseudocode, carte du code |
| **[CONTRIBUTING.md](CONTRIBUTING.md)** | Contributeurs — installation, standards et comment envoyer une bonne PR |
| **[docs/PROJECT_MANIFEST.md](docs/PROJECT_MANIFEST.md)** | La feuille de route complète, phase par phase |

---

## Contribuer

AILIENANT est open source et les contributions sont les bienvenues — de la correction d'une faute de frappe à la clôture d'un objectif de la feuille de route. Commencez par **[CONTRIBUTING.md](CONTRIBUTING.md)**.

Une chose à savoir d'emblée : comme le projet est sous double licence (voir ci-dessous), chaque contributeur signe un bref **[Accord de licence de contributeur (CLA)](CLA.md)** avant la fusion de sa première PR. C'est une étape unique et vous conservez le droit d'auteur sur votre travail.

---

## Licence

AILIENANT est **open-core et sous double licence** :

- **Édition Communauté — [GNU AGPL-3.0](LICENSE).** Libre d'utilisation, d'étude, de modification et de partage. Si vous la distribuez ou exécutez une version modifiée comme service réseau, vous partagez votre code source sous la même licence.
- **Édition Commerciale / Entreprise.** Pour les organisations qui ne peuvent pas accepter les termes de l'AGPL ou qui veulent des fonctionnalités et un support entreprise.

Voir **[LICENSING.md](LICENSING.md)** pour le tableau complet et comment obtenir une licence commerciale.

> Le nom **AILIENANT** et ses logos sont des marques du projet et ne sont pas couverts par l'AGPL.

---

<div align="center">

**Conçu pour les ingénieurs qui veulent un coéquipier IA en qui ils peuvent vraiment avoir confiance — et qu'ils peuvent auditer.**

Sur les épaules de <a href="https://github.com/langchain-ai/langgraph">LangGraph</a> · <a href="https://lancedb.com/">LanceDB</a> · <a href="https://tree-sitter.github.io/">Tree-sitter</a> · <a href="https://github.com/BerriAI/litellm">LiteLLM</a> · <a href="https://docs.pydantic.dev/">Pydantic</a>.

</div>
