# Pendeln Zuhause – Charité

Our own little commute page: bike to Potsdam Hbf, S7/RE1 to Wannsee, S1 to Botanischer Garten,
bike to the Charité – and back. It opens with the right direction for the time of day and shows the
next good connection on a platform-style display, with a countdown to when you have to leave.

* `index.html` – the page (runs entirely in the browser; live data from the VBB API, weather from Open-Meteo)
* `bahn_puffer.py` – the same logic on the command line, plus `--lernen` (records real delays and platforms)
* `.github/workflows/lernen.yml` – runs `--lernen` every 30 min, every day, and writes `data/delays.json`

## What "best connection" means here

1. It has to work: at least 90 % chance that you catch every train and arrive with it (not with the next one).
2. Then the earliest arrival, then the **shortest wait in the cold** at the transfer, then the later departure.
If a connection with less waiting but more risk arrives at the same time, the page offers it as
"Weniger frieren …" with its chance, and you decide.

**Changing in Wannsee** uses the walking time between the platforms as the minimum, never the 1-minute
same-platform special case. Wannsee has two S-Bahn island platforms (tracks 1/2 and 3/4) and the regional
platform (5/6), all linked by the pedestrian tunnel. Defaults (adjust under "Zeiten anpassen" after timing it once):
same platform 2 min, other S-Bahn platform through the tunnel 4 min, S-Bahn ↔ regional 5 min.
The platforms come from the live data, so a platform change also changes the required time.

## On the way

"Die nehm ich" starts live tracking of exactly those trains (every 30 s while the page is open):
where you should be right now, delays, platform changes, the time left to change in Wannsee.
If something breaks, the page plans from **where you are**: still at home → later connections from home;
on the bike or at the station → trains from Potsdam Hbf; sitting in the S7 and the S1 is gone → the next S1
from Wannsee. "Nehmen" switches to it. "Benachrichtigen" alerts you on cancellations, platform changes and
lost connections while the page is open (on iPhones only when added to the home screen).
Trips are kept under "Deine Fahrten" (on that device only).

## Setup (about 10 minutes)

1. Create a new **public** repository on GitHub, e.g. `pendeln`.
2. Upload all files from this folder, keeping the folders (`.github/workflows/`, `data/`).
3. *Settings → Pages*: Deploy from a branch, **main**, **/ (root)**. The page is then at `https://<you>.github.io/pendeln/`.
4. *Settings → Actions → General → Workflow permissions*: **Read and write permissions**.
5. *Actions* → "Verspaetungen lernen" → **Run workflow** once. After that it runs by itself; the
   statistics and the weekday × hour heatmap fill up over the following days.

Links: `…/pendeln/?von=zuhause` (to the Charité), `…/pendeln/?von=charite` (home), `…/pendeln/?demo=1`.
Two looks: amber station display (default) or blue platform display: `…/pendeln/?stil=db`, or switch under "Zeiten anpassen".
On the phone: open → Share → Add to Home Screen.

## Notes

* The page is public but only says "Zuhause" and "Charité"; no address.
* GitHub pauses scheduled workflows after 60 days without activity; re-enable under *Actions* if needed.
* All percentages come from 2000 simulated trips per refresh. They react to the current situation
  (how the lines run right now, disruption notices, weather) and get more accurate as real data comes in.
