# colector.py: registra líneas prepartido, sigue partidos en vivo y guarda resultados (CSV + Firestore)
import os
import json
import time
import statistics
import datetime as dt
import pandas as pd
import requests
import firebase_admin
from firebase_admin import credentials, firestore, messaging

API_KEY = os.environ["API_KEY"]
HEADERS = {"x-apisports-key": API_KEY}
BASE = "https://v1.basketball.api-sports.io"
REG = "registro_en_vivo.csv"
SEASON = "2026-2027"
COLECCION = "juegos"
EN_JUEGO = {"Q1", "Q2", "Q3", "Q4", "OT", "HT", "BT"}
CUARTOS_VIVO = {"Q1", "Q2", "Q3", "Q4", "OT"}
UMBRAL = 5.0
MIN_MINUTOS_VIVO = 12.0  # minutos jugados mínimos para avisar cambios de ritmo en vivo
TOPICO = "senales_nba"

# regla v2: proyección = 1ª mitad × 2 × factor, según si el partido es reñido al medio tiempo
UMBRAL_RENIDO = 8
FACTOR_RENIDO = 0.973
FACTOR_ABIERTO = 0.992
VERSION_REGLA = "v2"

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


def marcador(g):
    h, a = g["scores"]["home"], g["scores"]["away"]

    # la primera mitad solo se guarda cuando ya terminó el segundo cuarto (medio tiempo o después)
    mitad_lista = g["status"].get("short") in {"HT", "BT", "Q3", "Q4", "OT", "FT"}

    def mitad(lado):
        if not mitad_lista:
            return None
        q1, q2 = lado.get("quarter_1"), lado.get("quarter_2")
        return None if q1 is None or q2 is None else int(q1) + int(q2)

    def cuartos(lado):
        return {f"q{i}": (None if lado.get(f"quarter_{i}") is None else int(lado[f"quarter_{i}"]))
                for i in range(1, 5)}

    pl = int(h["total"] or 0)
    pv = int(a["total"] or 0)
    ml, mv = mitad(h), mitad(a)
    ql, qv = cuartos(h), cuartos(a)
    return {
        "pts_local": pl,
        "pts_visita": pv,
        "pts_actual": pl + pv,
        "mitad_local": ml,
        "mitad_visita": mv,
        "pts_Q1": None if ml is None or mv is None else ml + mv,
        "pts_Q2": None,
        # puntos por cuarto de cada equipo
        **{f"{k}_local": v for k, v in ql.items()},
        **{f"{k}_visita": v for k, v in qv.items()},
        # datos sin procesar del reloj, para calcular el ritmo en vivo
        "reloj_raw": g["status"].get("timer"),
        "cuarto_raw": g["status"].get("short"),
        "status_raw": str(g["status"]),
    }


def minutos_jugados(cuarto, reloj):
    """Minutos transcurridos del partido, según el cuarto y el reloj restante (MM:SS)."""
    if cuarto in ("HT", "BT"):
        return 24.0
    cuartos = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}
    if cuarto not in cuartos or not reloj:
        return None
    try:
        m, s = str(reloj).split(":")
        restante = int(m) + int(s) / 60
    except (ValueError, AttributeError):
        return None
    if not 0 <= restante <= 12:
        return None
    return (cuartos[cuarto] - 1) * 12 + (12 - restante)


def ritmo_en_vivo(g, total):
    minutos = minutos_jugados(g["status"].get("short"), g["status"].get("timer"))
    if minutos is None or minutos < 3:
        return None, None
    return minutos, round(total / minutos * 48, 1)


def ritmo_ht_ajustado(datos):
    """Proyección al final del partido: 1ª mitad × 2 × factor, según si el partido es reñido al medio tiempo."""
    ml, mv = datos.get("mitad_local"), datos.get("mitad_visita")
    if ml is None or mv is None:
        return None, None
    grupo = "reñido" if abs(ml - mv) <= UMBRAL_RENIDO else "abierto"
    factor = FACTOR_RENIDO if grupo == "reñido" else FACTOR_ABIERTO
    return (ml + mv) * 2 * factor, grupo


def senal_en_vivo(ritmo, linea):
    if ritmo >= linea + UMBRAL:
        return "OVER"
    if ritmo <= linea - UMBRAL:
        return "UNDER"
    return "NINGUNA"


def registrar_medio_tiempo(datos, previo):
    """Guarda la señal de la regla v2 al medio tiempo, con la línea mediana."""
    ritmo, _ = ritmo_ht_ajustado(datos)
    linea = previo.get("linea_mediana")
    if ritmo is None or linea is None:
        return
    datos["ritmo_ht"] = round(ritmo, 1)
    datos["senal_ht"] = senal_en_vivo(ritmo, linea)
    datos["version_regla"] = VERSION_REGLA


def notificar_senal_medio_tiempo(g, datos, previo):
    """Envía una notificación si al medio tiempo la proyección ajustada está a 5+ puntos de la línea mediana."""
    if previo.get("notificado_ht"):
        return
    ritmo, _ = ritmo_ht_ajustado(datos)
    linea = previo.get("linea_mediana")
    if ritmo is None or linea is None:
        return
    if abs(ritmo - linea) < UMBRAL:
        return
    lado = "OVER" if ritmo > linea else "UNDER"
    try:
        messaging.send(messaging.Message(
            notification=messaging.Notification(
                title=f"Señal al medio tiempo: {lado}",
                body=f"{g['teams']['away']['name']} @ {g['teams']['home']['name']} · ritmo {round(ritmo, 1)} vs línea {linea}",
            ),
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(channel_id="senales"),
            ),
            data={"juego_id": str(g["id"])},
            topic=TOPICO,
        ))
        datos["notificado_ht"] = True
    except Exception as e:
        print("No se pudo enviar la notificación:", e)


def notificar_cambio_ritmo(g, datos, previo, minutos):
    """Avisa una sola vez cuando el ritmo en vivo pasa de ninguna señal a OVER o UNDER."""
    ritmo = datos.get("ritmo_vivo")
    linea = previo.get("linea_mediana")
    if ritmo is None or linea is None or minutos is None or minutos < MIN_MINUTOS_VIVO:
        return
    lado = senal_en_vivo(ritmo, linea)
    datos["senal_vivo"] = lado
    if lado == "NINGUNA" or lado == previo.get("senal_vivo"):
        return
    try:
        messaging.send(messaging.Message(
            notification=messaging.Notification(
                title=f"Ritmo en vivo: {lado}",
                body=f"{g['teams']['away']['name']} @ {g['teams']['home']['name']} · ritmo {ritmo} vs línea {linea} · min {round(minutos)}",
            ),
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(channel_id="senales"),
            ),
            data={"juego_id": str(g["id"])},
            topic=TOPICO,
        ))
    except Exception as e:
        print("No se pudo enviar la notificación de ritmo:", e)


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
            datos = {"estado": estado, "actualizado_utc": ahora, **marcador(g)}
            minutos, ritmo = ritmo_en_vivo(g, datos["pts_actual"])
            if ritmo is not None:
                datos["minutos_jugados"] = minutos
                datos["ritmo_vivo"] = ritmo
            if estado == "FT":
                datos["pts_final"] = datos["pts_actual"]
            ref = db.collection(COLECCION).document(str(g["id"]))
            previo = ref.get().to_dict() or {}
            if estado in {"HT", "BT"}:
                if "senal_ht" not in previo:
                    registrar_medio_tiempo(datos, previo)
                notificar_senal_medio_tiempo(g, datos, previo)
            elif estado in CUARTOS_VIVO:
                notificar_cambio_ritmo(g, datos, previo, minutos)
            ref.set(datos, merge=True)
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
                m = marcador(g)
                total = m["pts_actual"]
                mask = df["id"] == g["id"]
                df.loc[mask, "pts_final"] = total
                df.loc[mask, "pts_Q1"] = m["pts_Q1"]
                db.collection(COLECCION).document(str(g["id"])).set(
                    {**m, "pts_final": total, "estado": "FT"}, merge=True)
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
