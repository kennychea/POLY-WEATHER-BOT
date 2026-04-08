# Phase 7 — Advanced Weather Models

## Goal
Upgrade the weather probability engine from naive member counting to a calibrated, multi-model, spread-aware system that produces sharper edges and better-sized bets.

## Current State (Post Phase 6)
- 3 NWP models fetched: GFS (31), ECMWF (51), ICON (40) = 122 total members
- **BUT main.py still uses single-model GFS only** — multi-model only in cli_scanner.py
- Probability: simple member counting (members_in_range / total_members)
- Confidence: binary high/medium/low from spread + inter-model disagreement
- Only temperature markets (high/low). No precipitation, snow, or wind.
- No historical calibration or model weighting

## Key Problems
1. Production pipeline ignores 2/3 of available data (ECMWF + ICON)
2. Equal-weight model averaging despite ECMWF being a superior model
3. No probability calibration — raw member count != true probability
4. Ensemble spread not used for bet sizing (only for coarse confidence label)
5. Missing precipitation/snow market opportunity

## Phase Scope
- P7.1: Wire multi-model into main.py production pipeline
- P7.2: Skill-weighted model averaging (configurable weights)
- P7.3: Spread-based confidence → Kelly fraction adjustment
- P7.4: Precipitation ensemble + market parsing
- P7.5: Historical calibration tracking (Brier score log → weight tuning)

## Out of Scope
- Full Platt scaling (needs months of data)
- Seasonal/geographic bias correction (complex, low ROI now)
- Wind markets (no Polymarket demand currently)
