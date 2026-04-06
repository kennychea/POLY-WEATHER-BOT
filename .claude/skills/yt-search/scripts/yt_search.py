#!/usr/bin/env python3
"""
YouTube Search Script — yt-search skill
Recherche YouTube par mots-cles via yt-dlp (mode flat-playlist).
Retourne une liste formatee avec URLs pretes pour NotebookLM.

Usage:
    python3 yt_search.py "query"

Dependencies:
    - yt-dlp (pip3 install yt-dlp)
    - Python 3.6+
"""

import json
import subprocess
import sys

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

MAX_RESULTS = 10
YT_BASE_URL = "https://www.youtube.com/watch?v="


# ---------------------------------------------------------------------------
# Formatage
# ---------------------------------------------------------------------------

def format_views(view_count):
    """Formate le nombre de vues avec des separateurs (espace fine insecable).

    Exemples:
        1234567  -> "1 234 567"
        None     -> "N/A"
        0        -> "0"
    """
    if view_count is None:
        return "N/A"
    n = int(view_count)
    if n == 0:
        return "0"
    s = str(n)
    groups = []
    while s:
        groups.append(s[-3:])
        s = s[:-3]
    return "\u202f".join(reversed(groups))


def format_duration(seconds):
    """Formate la duree en HH:MM:SS ou MM:SS.

    Exemples:
        3661  -> "1:01:01"
        125   -> "2:05"
        None  -> "N/A"
    """
    if seconds is None:
        return "N/A"
    seconds = int(seconds)
    if seconds < 0:
        return "N/A"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def sanitize_for_markdown(text):
    """Echappe les caracteres qui cassent les tableaux markdown."""
    if not text:
        return "N/A"
    return text.replace("|", "\\|").replace("\n", " ")


# ---------------------------------------------------------------------------
# Recherche
# ---------------------------------------------------------------------------

def search_youtube(query):
    """Recherche YouTube via yt-dlp en mode flat-playlist.

    Args:
        query: Mots-cles de recherche

    Returns:
        Liste de dicts avec les infos de chaque video

    Raises:
        SystemExit: Si yt-dlp echoue
    """
    cmd = [
        "yt-dlp",
        f"ytsearch{MAX_RESULTS}:{query}",
        "--flat-playlist",
        "--dump-json",
        "--no-warnings",
        "--quiet",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        print("ERREUR: yt-dlp n'est pas installe.", file=sys.stderr)
        print("Installation: pip3 install yt-dlp", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("ERREUR: Timeout — la recherche a pris plus de 30 secondes.", file=sys.stderr)
        print("Verifiez votre connexion internet.", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        print(f"ERREUR yt-dlp (code {result.returncode}):", file=sys.stderr)
        print(result.stderr.strip(), file=sys.stderr)
        sys.exit(1)

    stdout = result.stdout.strip()
    if not stdout:
        return []

    videos = []
    for line in stdout.split("\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        video_id = data.get("id", "")
        if not video_id:
            continue

        videos.append({
            "title": sanitize_for_markdown(data.get("title", "Sans titre")),
            "url": f"{YT_BASE_URL}{video_id}",
            "channel": sanitize_for_markdown(
                data.get("channel") or data.get("uploader") or "N/A"
            ),
            "views": format_views(data.get("view_count")),
            "duration": format_duration(data.get("duration")),
        })

    return videos


# ---------------------------------------------------------------------------
# Affichage
# ---------------------------------------------------------------------------

def print_results(videos):
    """Affiche les resultats sous forme de liste lisible."""
    if not videos:
        print("Aucun resultat trouve pour cette recherche.")
        return

    for i, v in enumerate(videos, 1):
        print(f"### {i}. {v['title']}")
        print(f"- URL: {v['url']}")
        print(f"- Chaine: {v['channel']}")
        print(f"- Vues: {v['views']}")
        print(f"- Duree: {v['duration']}")
        if i < len(videos):
            print()
            print("---")
            print()

    print()
    print(f"**{len(videos)} resultats** — URLs pretes a copier dans NotebookLM.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("Usage: python3 yt_search.py \"mots cles\"", file=sys.stderr)
        print("Exemple: python3 yt_search.py \"python tutorial\"", file=sys.stderr)
        sys.exit(1)

    query = sys.argv[1].strip()
    videos = search_youtube(query)
    print_results(videos)


if __name__ == "__main__":
    main()
