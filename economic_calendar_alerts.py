name: Calendrier Economique XAUUSD

on:
  schedule:
    - cron: "*/15 * * * *"
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: xauusd-economic-calendar
  cancel-in-progress: false

jobs:
  check-calendar:
    runs-on: ubuntu-latest
    env:
      PYTHONUNBUFFERED: "1"
    steps:
      - name: Récupérer le dépôt
        uses: actions/checkout@v4

      - name: Installer Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Installer les dépendances
        run: pip install -r requirements.txt

      - name: Lancer la vérification du calendrier économique
        env:
          BOT_TOKEN: ${{ secrets.BOT_TOKEN }}
          CHANNEL_ID: ${{ secrets.CHANNEL_ID }}
        run: python -u economic_calendar_alerts.py

      - name: Sauvegarder l'état (economic_calendar_state.json) dans le dépôt
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add economic_calendar_state.json
          git diff --quiet --cached || git commit -m "Mise a jour de l'etat du calendrier economique"

          # Si le push est rejeté (un autre run a modifié le dépôt entre-temps),
          # on récupère les derniers changements et on réessaie.
          for i in 1 2 3 4 5; do
            if git push; then
              echo "Push réussi (tentative $i)."
              break
            fi
            echo "Push rejeté (tentative $i/5), récupération des derniers changements..."
            git pull --rebase --autostash
            sleep $((RANDOM % 5 + 1))
          done
