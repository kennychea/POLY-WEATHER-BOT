---
name: yt-search
description: Recherche YouTube par mots-cles et retourne une liste formatee d'URLs. Use when user says "/yt-search", "recherche YouTube", "chercher des videos", "find YouTube videos", "search YouTube for", or asks to find YouTube content for NotebookLM. Do NOT use for YouTube trending, analytics, or channel management.
allowed-tools: "Bash(python3:*)"
metadata:
  author: frutz
  version: 1.0.0
  category: research
  tags: [youtube, search, notebooklm, yt-dlp]
---

# YouTube Search Skill

Recherche YouTube par mots-cles via yt-dlp et retourne une liste formatee de 10 resultats avec URLs pretes a copier dans NotebookLM.

## Important

- Ce skill utilise `yt-dlp` en mode flat-playlist (rapide, ~5-10s)
- Les donnees disponibles en mode flat sont : titre, id, chaine, vues, duree
- Les likes, commentaires et dates de publication ne sont PAS disponibles en mode flat
- Le script est autonome : il appelle yt-dlp, parse le JSON, et formate le tableau

## Instructions

### Step 1: Extraire la query

Extraire les mots-cles de recherche depuis l'argument passe par l'utilisateur.

Le format attendu est : `/yt-search "mots cles ici"`

Si l'utilisateur n'a pas fourni de query, lui demander quels mots-cles rechercher.

### Step 2: Executer le script de recherche

Depuis la racine du projet, executer :

```bash
python3 .claude/skills/yt-search/scripts/yt_search.py "<query>"
```

Ou `<query>` est la recherche fournie par l'utilisateur. Toujours entourer la query de guillemets doubles pour gerer les espaces.

Sortie attendue : une liste formatee avec pour chaque video : titre, URL, chaine, vues, duree.

### Step 3: Afficher les resultats

Afficher la liste retournee par le script telle quelle, sans modification.

Ajouter sous la liste :
- Le nombre de resultats retournes
- Un rappel que les URLs sont pretes a etre copiees dans NotebookLM
- Si l'utilisateur veut affiner, lui proposer de relancer avec d'autres mots-cles

### Step 4: Gestion des erreurs

Si le script retourne une erreur :
1. Verifier que `yt-dlp` est installe : `pip3 show yt-dlp`
2. Si pas installe, proposer : `pip3 install yt-dlp`
3. Si yt-dlp est installe mais echoue, verifier la connexion internet
4. Si la query ne retourne aucun resultat, suggerer des mots-cles alternatifs

## Examples

### Example 1: Recherche simple

User says: `/yt-search "machine learning basics"`

Actions:
1. Extraire la query : "machine learning basics"
2. Executer : `python3 .claude/skills/yt-search/scripts/yt_search.py "machine learning basics"`
3. Afficher la liste de 10 resultats

Result: Liste formatee avec 10 videos YouTube pertinentes

### Example 2: Recherche en francais

User says: `/yt-search "tutoriel python debutant"`

Actions:
1. Extraire la query : "tutoriel python debutant"
2. Executer le script
3. Afficher la liste

Result: Liste avec des videos en francais et anglais selon la pertinence YouTube

### Example 3: Sans argument

User says: `/yt-search`

Actions:
1. Detecter l'absence de query
2. Demander a l'utilisateur : "Quels mots-cles veux-tu rechercher sur YouTube ?"

## Troubleshooting

### Error: "command not found: yt-dlp"
Cause: yt-dlp n'est pas installe
Solution: Executer `pip3 install yt-dlp`

### Error: "ERROR: Unable to extract"
Cause: yt-dlp a besoin d'une mise a jour (YouTube change regulierement son API)
Solution: Executer `pip3 install -U yt-dlp`

### Error: Liste vide ou 0 resultats
Cause: La query ne correspond a aucune video ou probleme reseau
Solution: Verifier la connexion internet, essayer des mots-cles differents

### Error: "No module named json"
Cause: Python3 n'est pas installe correctement
Solution: Verifier l'installation de Python3 avec `python3 --version`
