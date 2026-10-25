# analisis_factor.py: calcula el factor de ajuste de la regla al medio tiempo con partidos terminados
import os
import json
import firebase_admin
from firebase_admin import credentials, firestore

UMBRAL_RENIDO = 8  # diferencia al medio tiempo para considerar el partido reñido

firebase_admin.initialize_app(credentials.Certificate(json.loads(os.environ["FIREBASE_KEY"])))
db = firestore.client()


def num(d, k):
    v = d.get(k)
    return None if v is None else int(v)


grupos = {"reñido": [], "abierto": []}
omitidos = 0

for doc in db.collection("juegos").where("estado", "==", "FT").stream():
    d = doc.to_dict()
    q = {}
    for i in range(1, 5):
        l, v = num(d, f"q{i}_local"), num(d, f"q{i}_visita")
        if l is None or v is None:
            break
        q[i] = (l, v)
    if len(q) < 4:
        omitidos += 1
        continue

    mitad1 = sum(q[i][0] + q[i][1] for i in (1, 2))
    mitad2 = sum(q[i][0] + q[i][1] for i in (3, 4))
    prom_1_3 = sum(q[i][0] + q[i][1] for i in (1, 2, 3)) / 3
    if mitad1 == 0 or prom_1_3 == 0:
        omitidos += 1  # registro incompleto o con ceros
        continue

    margen_medio = abs(sum(q[i][0] for i in (1, 2)) - sum(q[i][1] for i in (1, 2)))
    grupo = "reñido" if margen_medio <= UMBRAL_RENIDO else "abierto"
    cuarto4 = q[4][0] + q[4][1]
    grupos[grupo].append((mitad1, mitad2, cuarto4, prom_1_3))

total = sum(len(v) for v in grupos.values())
print(f"Partidos usados: {total} | omitidos: {omitidos}")
print()

for nombre, filas in grupos.items():
    if not filas:
        print(f"{nombre}: sin datos")
        continue
    m1 = sum(f[0] for f in filas)
    m2 = sum(f[1] for f in filas)
    k = (m1 + m2) / (2 * m1)
    ratio_q4 = sum(f[2] / f[3] for f in filas) / len(filas)
    print(f"{nombre}: n={len(filas)}")
    print(f"  promedio 1ª mitad: {m1 / len(filas):.1f} | promedio total: {(m1 + m2) / len(filas):.1f}")
    print(f"  factor k: {k:.3f}  (proyección = 1ª mitad × 2 × k)")
    print(f"  cuarto 4 vs promedio de cuartos 1-3: {ratio_q4:.3f}")
    print()
