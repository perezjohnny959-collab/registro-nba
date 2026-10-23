name: NBA Over/Under

on:
  schedule:
    - cron: "0 12 * * *"          # registro de líneas
    - cron: "0 6 * * *"           # resultados finales
    - cron: "*/10 22-23,0-7 * * *"  # en vivo (6:00 pm a 3:59 am hora RD)
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: nba-colector
  cancel-in-progress: false

jobs:
  colector:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Instalar dependencias
        run: pip install requests pandas firebase-admin

      - name: Ejecutar colector
        env:
          API_KEY: ${{ secrets.API_SPORTS_KEY }}
          FIREBASE_KEY: ${{ secrets.FIREBASE_KEY }}
          MODO: ${{ github.event.schedule == '0 6 * * *' && 'resultados' || github.event.schedule == '0 12 * * *' && 'registro' || github.event_name == 'workflow_dispatch' && 'registro' || 'vivo' }}
        run: python colector.py

      - name: Guardar registro en el repositorio
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add registro_en_vivo.csv
          git diff --cached --quiet || git commit -m "Actualiza registro NBA"
          git push
