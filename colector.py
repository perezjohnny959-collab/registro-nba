# colector.py: registra líneas prepartido, sigue partidos en vivo y guarda resultados (CSV + Firestore)
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
EN_JUEGO = {"Q1", "Q2", "Q3", "Q4", "OT", "HT", "BT"}

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


def puntos_por_cuarto(g):
    h, a = g["scores"]["home"], g["scores"]["away"]

    def suma(q):
        if h.get(q) is None or a.get(q) is None:
            return None
        return int(h[q]) + int(a[q])

    return suma("quarter_1"), suma("quarter_2")


def total_actual(g):
    return int(g["scores"]["home"]["total"] or 0) + int(g["scores"]["away"]["total"] or 0)


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


def ids_pendientes():
    if not os.path.exists(REG):
        return set()
    df = pd.read_csv(REG)
    return set(df.loc[df["pts_final"].isna(), "id"].astype(int))


def actualizar_en_vivo():
    pendientes = ids_pendientes()
    if not pendientes:
        print("No hay partidos pendientes.")
        return
    hoy = ahora_utc().date()
    fechas = sorted({hoy.isoformat(), (hoy - dt.timedelta(days=1)).isoformat()})
    ahora = ahora_utc().isoformat(timespec="minutes")
    actualizados = 0
    for f in fechas:
        for g in juegos_del_dia(f):
            if g["id"] not in pendientes:
                continue
            estado = g["status"]["short"]
            if estado != "FT" and estado not in EN_JUEGO:
                continue
            q1, q2 = puntos_por_cuarto(g)
            datos = {
                "estado": estado,
                "pts_Q1": q1,
                "pts_Q2": q2,
                "pts_actual": total_actual(g),
                "actualizado_utc": ahora,
            }
            if estado == "FT":
                datos["pts_final"] = total_actual(g)
            db.collection(COLECCION).document(str(g["id"])).set(datos, merge=True)
            actualizados += 1
        time.sleep(0.5)
    print("Partidos actualizados en vivo:", actualizados)


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
                total = total_actual(g)
                q1, q2 = puntos_por_cuarto(g)
                df.loc[df["id"] == g["id"], "pts_final"] = total
                df.loc[df["id"] == g["id"], "pts_Q1"] = q1
                df.loc[df["id"] == g["id"], "pts_Q2"] = q2
                db.collection(COLECCION).document(str(g["id"])).set({
                    "pts_final": total,
                    "pts_actual": total,
                    "pts_Q1": q1,
                    "pts_Q2": q2,
                    "estado": "FT",
                }, merge=True)
                actualizados += 1
        time.sleep(0.5)
    df.to_csv(REG, index=False)
    print("Resultados actualizados:", actualizados, "| pendientes:", int(df["pts_final"].isna().sum()))


if __name__ == "__main__":
    modo = os.environ.get("MODO", "registro")
    if modo == "resultados":
        actualizar_resultados()
    elif modo == "vivo":
        actualizar_en_vivo()
    else:
        registrar(1)
