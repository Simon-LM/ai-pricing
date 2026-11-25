# AGENTS.md — dépôt ai-pricing

Dépôt GitHub public qui publie les tarifs des API d'IA (`pricing.json` + schéma + docs).

- Source de vérité : `pricing.json`, validé par `pricing.schema.json`.
- Docs : `docs/` (format, how-it-works, contributing). PRs via `.github/pull_request_template.md`.
- Scripts fournisseurs : `scripts/providers/<nom>/` (chacun avec son README).
- Tests : `pytest` (dossier `tests/`). Toujours les faire passer avant commit.
- Style : pas de secrets ni de données personnelles dans le dépôt (il est public).
- Les consignes métier complètes seront rédigées ici par Lead AI Pricing.
