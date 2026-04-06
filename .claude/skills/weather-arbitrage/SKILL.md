---
name: weather-arbitrage
description: "Scanner d'arbitrage météo Polymarket — compare les probabilités Open-Meteo Ensemble (51 membres) aux prix du marché pour trouver des edges. Utiliser quand l'utilisateur dit 'scan les marchés', 'trouve des edges', 'arbitrage météo', 'scan weather', 'check opportunities', 'run the scanner', 'lance le scan', ou toute demande liée à la détection d'opportunités de trading météo sur Polymarket."
---

# Weather Arbitrage Scanner

Pipeline automatisé qui identifie des écarts de prix entre les modèles météo ensemble (Open-Meteo, 51 membres) et les prix actuels de Polymarket.

L'approche est inspirée de Moon Dev (scanner global + Open-Meteo) et Emil Nielsen (arbitrage de probabilité + consensus de modèles). L'avantage clé : au lieu d'approximer avec une CDF normale (ancien système OpenWeatherMap), on **compte directement** combien de membres ensemble prévoient chaque outcome.

## Architecture

```
Open-Meteo Ensemble API (51 membres, sans clé)
        ↓
  Probability par comptage direct
        ↓
  Polymarket CLOB (prix actuels)
        ↓
  Edge = P_modèle - P_marché
        ↓
  Filtrage (edge > seuil)
        ↓
  Rapport d'opportunités
```

## Instructions

### Step 1: Lancer le scan complet

Exécuter le script principal depuis la racine du projet :

```bash
python .claude/skills/weather-arbitrage/scripts/scan_edge.py
```

Le script va :
1. Scanner Polymarket pour les marchés météo actifs (villes US)
2. Fetcher les données ensemble Open-Meteo (51 membres) pour chaque ville
3. Calculer les probabilités par comptage direct des membres
4. Comparer avec les prix Polymarket
5. Afficher les opportunités triées par edge décroissant

### Step 2: Interpréter les résultats

Le script affiche un tableau avec :
- **Market** : la question Polymarket
- **City** : la ville concernée
- **Model P** : probabilité calculée par le modèle ensemble
- **Market P** : prix actuel sur Polymarket (implied probability)
- **Edge** : différence (Model P - Market P)
- **Signal** : BUY_YES ou BUY_NO selon le sens de l'edge
- **Confidence** : High/Medium/Low basé sur le consensus des membres

Un edge positif > 10% avec une confiance High est une opportunité forte.

### Step 3: Options avancées

```bash
# Scanner une ville spécifique
python .claude/skills/weather-arbitrage/scripts/scan_edge.py --city "New York"

# Changer le seuil d'edge minimum (défaut: 5%)
python .claude/skills/weather-arbitrage/scripts/scan_edge.py --min-edge 0.10

# Mode JSON pour intégration pipeline
python .claude/skills/weather-arbitrage/scripts/scan_edge.py --json

# Mode verbose (détails ensemble par bucket)
python .claude/skills/weather-arbitrage/scripts/scan_edge.py -v
```

### Step 4: Intégration avec le bot

Les résultats du scanner peuvent alimenter le pipeline existant du bot :
- Les signaux sont compatibles avec `WeatherSignal` dans `infra/types.py`
- Le risk manager (`core/risk.py`) peut sizing les positions
- Le paper trader peut exécuter les ordres

Pour intégrer dans le main loop, le module `weather/ensemble_fetcher.py` expose une API async réutilisable.

## Troubleshooting

### Pas de marchés trouvés
- Vérifier que Polymarket a des marchés météo actifs (ils sont saisonniers)
- Essayer avec `--verbose` pour voir les marchés scannés

### Erreur Open-Meteo
- L'API est gratuite et sans clé, mais rate-limitée
- Le script implémente un cache de 30 minutes
- Si timeout, réessayer dans quelques minutes

### Edge toujours faible
- Normal si le marché est efficient
- Les meilleurs edges apparaissent quand les prévisions changent rapidement
- Scanner plus fréquemment (toutes les 15 min) pour capturer les mouvements

## Villes supportées

15 villes US avec coordonnées hardcodées pour performance maximale (pas de geocoding) :
New York, Los Angeles, Chicago, Miami, Washington DC, Houston, Phoenix, Denver, Seattle, Boston, Atlanta, Dallas, San Francisco, Minneapolis, Detroit
