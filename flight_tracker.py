#!/usr/bin/env python3
"""
Bot de suivi de prix de vols Tunis (TUN) -> Malé (MLE)
Route: TUN -> MLE le 22/08/2026, retour MLE -> TUN le 01/09/2026
Passagers: 2 adultes + 2 enfants (14 ans et 12 ans), classe Affaires
"""

import os
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# ----------------------------------------------------------------------
# CONFIGURATION DU VOYAGE
# ----------------------------------------------------------------------
ORIGIN = "TUN"
DESTINATION = "MLE"
DEPART_DATE = "2026-08-22"
RETURN_DATE = "2026-09-01"
ADULTS = 2
CHILD_AGES = [14, 12]
CABIN_CLASS = "business"
CURRENCY = "EUR"
MARKET = "FR"

CHANGE_ALERT_THRESHOLD_PCT = 5.0

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = "sky-scrapper.p.rapidapi.com"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

HISTORY_FILE = Path(__file__).parent / "price_history.json"
AIRPORT_CACHE_FILE = Path(__file__).parent / "airport_cache.json"

BASE_URL = f"https://{RAPIDAPI_HOST}"
HEADERS = {
    "X-RapidAPI-Key": RAPIDAPI_KEY,
    "X-RapidAPI-Host": RAPIDAPI_HOST,
}


def fail(msg: str):
    print(f"[ERREUR] {msg}", file=sys.stderr)
    sys.exit(1)


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_sky_id(iata_code: str) -> dict:
    cache = load_json(AIRPORT_CACHE_FILE, {})
    if iata_code in cache:
        return cache[iata_code]

    resp = requests.get(
        f"{BASE_URL}/api/v1/flights/searchAirport",
        headers=HEADERS,
        params={"query": iata_code, "locale": "fr-FR"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    candidates = data.get("data", [])
    match = None
    for c in candidates:
        presentation = c.get("presentation", {})
        if presentation.get("suggestionTitle", "").upper().startswith(iata_code):
            match = c
            break
    if match is None and candidates:
        match = candidates[0]

    if match is None:
        fail(f"Impossible de résoudre l'aéroport '{iata_code}' via l'API.")

    nav = match["navigation"]
    result = {
        "skyId": match.get("skyId", iata_code),
        "entityId": nav.get("entityId"),
    }
    cache[iata_code] = result
    save_json(AIRPORT_CACHE_FILE, cache)
    return result


def search_cheapest_business_fare() -> dict:
    origin = resolve_sky_id(ORIGIN)
    destination = resolve_sky_id(DESTINATION)

    params = {
        "originSkyId": origin["skyId"],
        "destinationSkyId": destination["skyId"],
        "originEntityId": origin["entityId"],
        "destinationEntityId": destination["entityId"],
        "date": DEPART_DATE,
        "returnDate": RETURN_DATE,
        "adults": ADULTS,
        "childrenAges": ",".join(str(a) for a in CHILD_AGES),
        "cabinClass": CABIN_CLASS,
        "currency": CURRENCY,
        "market": MARKET,
        "countryCode": MARKET,
        "sortBy": "best",
    }

    resp = requests.get(
        f"{BASE_URL}/api/v2/flights/searchFlightsComplete",
        headers=HEADERS,
        params=params,
        timeout=60,
    )
    resp.raise_for_status()
    payload = resp.json()

    itineraries = (
        payload.get("data", {}).get("itineraries")
        or payload.get("data", {}).get("flights")
        or []
    )
    if not itineraries:
        return {"found": False, "raw": payload}

    cheapest = min(
        itineraries,
        key=lambda it: it.get("price", {}).get("raw", float("inf")),
    )

    price = cheapest.get("price", {}).get("raw")
    legs = cheapest.get("legs", [])
    airlines = []
    for leg in legs:
        for carrier in leg.get("carriers", {}).get("marketing", []):
            name = carrier.get("name")
            if name and name not in airlines:
                airlines.append(name)

    return {
        "found": True,
        "price": price,
        "currency": CURRENCY,
        "airlines": airlines,
        "raw_id": cheapest.get("id"),
    }


def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[INFO] Pas de config Telegram : message non envoyé, affiché ici à la place :")
        print(text)
        return

    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    resp.raise_for_status()


def main():
    if not RAPIDAPI_KEY:
        fail("RAPIDAPI_KEY manquant. Définis-le comme variable d'environnement / secret GitHub.")

    history = load_json(HISTORY_FILE, {"checks": []})
    now = datetime.now(timezone.utc).isoformat()

    try:
        result = search_cheapest_business_fare()
    except requests.HTTPError as e:
        fail(f"Erreur API vols ({e}). Vérifie ta clé RapidAPI et ton abonnement à Sky-Scrapper.")

    if not result.get("found"):
        print("[INFO] Aucun vol Affaires trouvé pour ces dates lors de cette vérification.")
        history["checks"].append({"time": now, "found": False})
        save_json(HISTORY_FILE, history)
        return

    price = result["price"]
    airlines = ", ".join(result["airlines"]) if result["airlines"] else "compagnie non précisée"

    previous_checks = [c for c in history["checks"] if c.get("found")]
    previous_price = previous_checks[-1]["price"] if previous_checks else None

    history["checks"].append(
        {"time": now, "found": True, "price": price, "currency": CURRENCY, "airlines": result["airlines"]}
    )
    save_json(HISTORY_FILE, history)

    trip_summary = (
        f"✈️ <b>Tunis → Malé</b> ({DEPART_DATE} → {RETURN_DATE})\n"
        f"Classe Affaires · 2 adultes + 2 enfants\n"
        f"Compagnie(s): {airlines}\n"
    )

    if previous_price is None:
        send_telegram_message(
            f"{trip_summary}\n"
            f"💰 Premier prix relevé : <b>{price:.0f} {CURRENCY}</b> (total)\n"
            f"Je te préviendrai à chaque baisse ou variation notable."
        )
        print(f"[OK] Premier relevé : {price} {CURRENCY}")
        return

    diff = price - previous_price
    pct = (diff / previous_price) * 100 if previous_price else 0

    if abs(pct) < CHANGE_ALERT_THRESHOLD_PCT:
        print(f"[OK] Prix stable : {price} {CURRENCY} (variation {pct:+.1f}%, pas d'alerte)")
        return

    if diff < 0:
        emoji, verdict = "🔻", "BAISSE"
    else:
        emoji, verdict = "🔺", "HAUSSE"

    send_telegram_message(
        f"{trip_summary}\n"
        f"{emoji} <b>{verdict} de prix</b> : {previous_price:.0f} → <b>{price:.0f} {CURRENCY}</b> "
        f"({pct:+.1f}%)"
    )
    print(f"[OK] Alerte envoyée : {verdict} {pct:+.1f}%")


if __name__ == "__main__":
    main()
