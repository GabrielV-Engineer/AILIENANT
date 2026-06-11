<!-- markdownlint-disable MD033 MD041 -->
<div align="center">

<img src="assets/logo.svg" alt="AILIENANT" width="340" />

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

- **🧠 Il planifie avant de coder.** Un *Planificateur* dédié transforme votre demande en une spécification concrète et une liste de tâches, fige le périmètre et surveille la « dérive » pour que l'agent ne s'égare pas en silence et ne réécrive pas la moitié de votre projet. Un *Codeur* distinct exécute ce plan. Deux têtes, chacune faisant bien une chose.
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
| Planifie puis code (bicéphale) | ✅ Planificateur + Codeur, avec garde de dérive | ❌ Un modèle, un essai |
| Routage intelligent local↔cloud | ✅ Choisit le niveau le moins cher qui convient | ❌ Fixe |
| Affiche le coût en temps réel | ✅ Registre de jetons + plafond budgétaire | ⚠️ Souvent caché |
| Voyage dans le temps / bifurquer une exécution | ✅ Points de contrôle durables | ❌ Sans état |
| Exécution en bac à sable | ✅ Docker / Wasm / soumise à validation | ⚠️ Souvent sur l'hôte |
| Verrouillage fournisseur | ✅ Aucun — changez librement | ❌ Lié à un seul |

Une comparaison technique plus complète se trouve dans **[HowItWorks.md](HowItWorks.md)**.

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

Ouvrez ensuite le projet dans VS Code et appuyez sur **F5** pour lancer l'extension. À la première ouverture d'une session AILIENANT, elle démarre le backend pour vous et commence à indexer votre workspace. Configurez vos modèles depuis le panneau **BYOM** intégré, saisissez une demande, et c'est parti.

---

## Comment ça marche (version courte)

```
Vous demandez ─▶ Planificateur ─▶ garde de ─▶ Codeur ─▶ le bac à sable l'exécute
                (rédige spec      dérive       (édite        ▲      │
                 + plan)          (périmètre    fichiers)     │      ▼
                                   verrouillé)            corrige ◀─ lit le résultat
```

En coulisses, un moteur **LangGraph** à états route chaque tâche entre modèles locaux et cloud à l'aide d'un score de contexte et de complexité, récupère les bons fichiers avec **GraphRAG** (recherche vectorielle + un parcours de dépendances d'un saut) et crée un point de contrôle à chaque étape pour ne rien perdre. La version approfondie — diagrammes, mathématiques du routage, boucle d'exécution et modèle de sécurité — est dans **[HowItWorks.md](HowItWorks.md)**.

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
