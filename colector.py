# colector.py: registra líneas prepartido y actualiza resultados (CSV + Firestore)
import os
import json
import time
import statistics
import datetime as dt
import pandas as pd
import requests
import firebase_admin
from firebase_admin import credentials, firestore

API_KEY = os.environ["API_KEY"]
HEADERS = {"x-apisports-key": API_KEY}
BASE = "https://v1.basketball.api-sports.io"
REG = "registro_en_vivo.csv"
SEASON = "2026-2027"
COLECCION = "juegos"

if not firebase_admin._apps:
    firebase_admin.initialize_app(credentials.Certificate(json.loads(os.environ["FIREBASE_KEY"])))
db = firestore.client()


def ahora_utc():
    return dt.datetime.now(dt.timezone.utc)


def juegos_del_dia(fecha):
    r = requests.get(f"{BASE}/games", headers=HEADERS,
                     params={"league": 12, "season": SEASON, "date": fecha}, timeout=30)
    r.raise_for_status()
    return r.json().get("response", [])


def linea_principal(bet):
    opciones = {}
    for v in bet["values"]:
        lado, pts = v["value"].split(" ", 1)
        opciones.setdefault(pts, {})[lado] = float(v["odd"])
    candidatos = [(abs(d["Over"] - d["Under"]), pts, d)
                  for pts, d in opciones.items() if "Over" in d and "Under" in d]
    if not candidatos:
        return None
    _, pts, d = min(candidatos)
    return float(pts), d["Over"], d["Under"]


def cuotas_juego(game_id):
    r = requests.get(f"{BASE}/odds", headers=HEADERS, params={"game": game_id}, timeout=30)
    r.raise_for_status()
    resp = r.json().get("response", [])
    if not resp:
        return None
    lineas, pinnacle = [], None
    for bm in resp[0].get("bookmakers", []):
        for bet in bm.get("bets", []):
            if bet["name"] != "Over/Under":
                continue
            res = linea_principal(bet)
            if res is None:
                break
            linea, over, under = res
            lineas.append(linea)
            if bm["name"] == "Pinnacle":
                pinnacle = (linea, over, under)
            break
    if not lineas:
        return None
    return {
        "linea_mediana": statistics.median(lineas),
        "n_casas": len(lineas),
        "linea_pinnacle": pinnacle[0] if pinnacle else None,
        "over_pinnacle": pinnacle[1] if pinnacle else None,
        "under_pinnacle": pinnacle[2] if pinnacle else None,
    }


def registrar(dias_adelante=1):
    filas = []
    hoy = ahora_utc().date()
    for d in range(dias_adelante + 1):
        fecha = (hoy + dt.timedelta(days=d)).isoformat()
        for g in juegos_del_dia(fecha):
            if g["status"]["short"] != "NS":
                continue
            c = cuotas_juego(g["id"])
            time.sleep(0.5)
            if c is None:
                continue
            fila = {
                "id": g["id"],
                "fecha_utc": g["date"],
                "equipo_local": g["teams"]["home"]["name"],
                "equipo_visita": g["teams"]["away"]["name"],
                "capturado_utc": ahora_utc().isoformat(timespec="minutes"),
                **c,
                "pts_final": None,
            }
            filas.append(fila)
            db.collection(COLECCION).document(str(g["id"])).set(fila, merge=True)
    if filas or os.path.exists(REG):
        nuevo = pd.DataFrame(filas)
        if os.path.exists(REG):
            viejo = pd.read_csv(REG)
            nuevo = pd.concat([viejo, nuevo]).drop_duplicates(subset="id", keep="last")
        nuevo.to_csv(REG, index=False)
    print("Juegos registrados:", len(filas))


def actualizar_resultados():
    if not os.path.exists(REG):
        print("No hay archivo de registro todavía.")
        return
    df = pd.read_csv(REG)
    pend = df[df["pts_final"].isna()]
    fechas = sorted(set(pd.to_datetime(pend["fecha_utc"]).dt.date.astype(str)))
    actualizados = 0
    for f in fechas:
        for g in juegos_del_dia(f):
            if g["status"]["short"] == "FT":
                total = int(g["scores"]["home"]["total"] + g["scores"]["away"]["total"])
                df.loc[df["id"] == g["id"], "pts_final"] = total
                db.collection(COLECCION).document(str(g["id"])).set({"pts_final": total}, merge=True)
                actualizados += 1
        time.sleep(0.5)
    df.to_csv(REG, index=False)
    print("Resultados actualizados:", actualizados, "| pendientes:", int(df["pts_final"].isna().sum()))


if __name__ == "__main__":
    if os.environ.get("MODO", "registro") == "resultados":
        actualizar_resultados()
    else:
        registrar(1)
