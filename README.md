# Soehnle Airclean Connect 500

Home-Assistant-Custom-Integration fuer den Soehnle `Airclean Connect 500 (AC500)` ueber Bluetooth.

Lizenz: MIT. Copyright (c) 2026 Narolinus `<git@narolinus.de>`.

## Kurzuberblick

Die Integration bindet Soehnle-AC500-Luftfilter direkt in Home Assistant ein, einschliesslich connectabler Bluetooth-Proxies wie ESPHome- oder Shelly-basierten Geraeten. Einrichtung, Pairing und laufende Kommunikation passieren direkt aus Home Assistant heraus.

Merkmale:

- Bluetooth-Discovery fuer `AC500`
- Einrichtung direkt aus Home Assistant heraus, ohne externes Skript
- Pairing per `Pair`-Aktion direkt aus Home Assistant heraus
- Hinzufuegen entweder ueber sichtbare Bluetooth-Geraete oder ueber manuelle Eingabe der MAC-Adresse
- aktive BLE-Verbindungen ueber Home Assistants Bluetooth-Abstraktion
- funktioniert damit auch mit connectablen Bluetooth-Proxies wie ESPHome- oder Shelly-basierten Controllern
- trennt sauber zwischen Status-Abruf und aktivem Control Mode:
  - Status-Polls oeffnen nur kurz den Datenkanal und holen einen Live-Frame
  - Steuerbefehle oeffnen nur fuer den jeweiligen Befehl den eigentlichen Control Mode
- legt jeden Luftfilter als eigenes Geraet mit diesen Entitaeten an:
  - `fan`: Ein/Aus und Luefterstufe
  - `select`: Timer
  - `switch`: UV-C, Auto, Night, Buzzer
  - `sensor`: PM2.5, Temperatur, Filterlebensdauer, RSSI, Session-Status

## Installation

### HACS

Die Integration kann in HACS als benutzerdefiniertes Repository vom Typ `Integration` hinzugefuegt werden:

- Repository: `https://github.com/narolinus/soehnle-ac500`
- Kategorie: `Integration`

### Manuell

Ordnerstruktur in Home Assistant:

```text
config/
  custom_components/
    soehnle_ac500/
      ...
```

Danach Home Assistant neu starten und die Integration ueber `Einstellungen -> Geraete & Dienste` hinzufuegen. Bereits per Bluetooth entdeckte AC500-Geraete sollten auch automatisch als neue Integration vorgeschlagen werden.

## Einrichtung

Das Hinzufuegen legt zunaechst nur den Eintrag an. Falls fuer Schreibbefehle noch kein Bonding bzw. proprietaerer AC500-Handshake vorhanden ist, wird das danach ueber die `Pair`-Aktion gestartet. Dabei waehrend der laufenden Aktion die Bluetooth-Taste am Geraet druecken.

Der Einrichtungsfluss unterstuetzt:

- Auswahl bereits sichtbarer AC500-Geraete
- manuelle Eingabe der Bluetooth-MAC-Adresse
- automatische Bluetooth-Discovery fuer noch nicht eingerichtete sichtbare Geraete

## Laufzeitverhalten

Die Integration behandelt die beiden beobachteten BLE-Betriebsarten bewusst getrennt:

- Status-Abruf: fuer regelmaessige Updates wird eine kurze BLE-Session aufgebaut, `AF 00 01` zur Initialisierung gesendet und danach mit `A2 00 03` ein Live-Status angefordert.
- Control Mode: vor Steuerbefehlen wird eine eigene Control-Session mit zweimal `AF 00 01` aufgebaut. Nur in dieser Phase soll das Geraet Kommandos annehmen; das ist am AC500 an der dauerhaft leuchtenden LED erkennbar.
- Pairing: die `Pair`-Aktion versucht zuerst normales BLE-Bonding und danach den proprietaeren `EF03`-Handshake (`A2 00 03` -> Ack `A2 00 02` -> `A2 00 01`).

Dadurch bleibt die LED im Normalbetrieb nicht dauerhaft an, und trotzdem koennen Statusdaten sowie Steuerbefehle sauber ueber Home Assistants Bluetooth-Stack und Proxies abgewickelt werden.

## Entwicklung

```bash
python3 -m pip install -r requirements.txt
```

Unter Linux benoetigt `bleak` ein funktionierendes BlueZ-Setup. Je nach System muss das Skript mit passenden Bluetooth-Berechtigungen laufen.

## Nutzung

Zunaechst nach dem Geraet suchen:

```bash
python3 ac500_cli.py scan
```

Status lesen:

```bash
python3 ac500_cli.py --address E0:07:18:02:29:89 status
```

Falls Steuerbefehle scheitern, zuerst einmalig pairen. Dazu den AC500 mit der Bluetooth-Taste in den Pairing-Modus bringen und dann:

```bash
python3 ac500_cli.py --address E0:07:18:02:29:89 pair
```

Wichtig: Der `pair`-Befehl macht nicht nur das normale BLE-Bonding, sondern auch den in den Mitschnitten beobachteten AC500-Handshake ueber `EF03`:

```text
write A2 00 03
warte auf EF03-Notify A2 00 02
write A2 00 01
```

Den Bluetooth-Knopf am Geraet also waehrend des laufenden `pair`-Befehls druecken.

Danach koennen Schreibbefehle bei Bedarf auch mit Pairing-vor-dem-Connect gestartet werden:

```bash
python3 ac500_cli.py --address E0:07:18:02:29:89 --pair fan turbo
```

Wenn das Geraet nur bei offener Steuer-Session reagiert, kann die Verbindung jetzt wie in den Mitschnitten beobachtet offen gehalten werden:

```bash
python3 ac500_cli.py --address E0:07:18:02:29:89 --verbose session
```

Danach koennen direkt in derselben BLE-Verbindung Befehle eingegeben werden:

```text
status
auto off
fan turbo
uv on
timer 2
quit
```

Luefter steuern:

```bash
python3 ac500_cli.py --address E0:07:18:02:29:89 fan low
python3 ac500_cli.py --address E0:07:18:02:29:89 fan medium
python3 ac500_cli.py --address E0:07:18:02:29:89 fan high
python3 ac500_cli.py --address E0:07:18:02:29:89 fan turbo
```

Die Schreibbefehle verwenden intern einen eigenen "Control-Session"-Ablauf, der den Bluetooth-Mitschnitt nachbildet.

Falls ein Einmal-Befehl nur mit etwas laenger geoeffneter Verbindung greift, kann die Session nach dem Write bewusst noch kurz offen bleiben:

```bash
python3 ac500_cli.py --address E0:07:18:02:29:89 --hold-seconds 5 fan turbo
```

Timer steuern:

```bash
python3 ac500_cli.py --address E0:07:18:02:29:89 timer off
python3 ac500_cli.py --address E0:07:18:02:29:89 timer 2
python3 ac500_cli.py --address E0:07:18:02:29:89 timer 4
python3 ac500_cli.py --address E0:07:18:02:29:89 timer 8
```

Weitere Schalter:

```bash
python3 ac500_cli.py --address E0:07:18:02:29:89 power on
python3 ac500_cli.py --address E0:07:18:02:29:89 power off
python3 ac500_cli.py --address E0:07:18:02:29:89 uv on
python3 ac500_cli.py --address E0:07:18:02:29:89 uv off
python3 ac500_cli.py --address E0:07:18:02:29:89 night on
python3 ac500_cli.py --address E0:07:18:02:29:89 night off
python3 ac500_cli.py --address E0:07:18:02:29:89 auto on
python3 ac500_cli.py --address E0:07:18:02:29:89 auto off
python3 ac500_cli.py --address E0:07:18:02:29:89 buzzer off
python3 ac500_cli.py --address E0:07:18:02:29:89 buzzer on
```

Rohdaten fuer Reverse Engineering:

```bash
python3 ac500_cli.py decode-frame aa0da02100008b000a00cb10e0045375ee
python3 ac500_cli.py --address E0:07:18:02:29:89 history-dump --seconds 5
python3 ac500_cli.py --address E0:07:18:02:29:89 raw 0x05 0x00 0x01
```

## Aktueller Stand der Protokoll-Zuordnung

- `fan` und `timer` .
- `power`, `fan`, `uv`, `timer`, `auto` und `night` 
- `buzzer`
- Beim Wechsel von `auto` auf eine feste Luefterstufe zeigen die Mitschnitte zuerst `auto off` und danach erst den Fan-Befehl.
- `pm2.5` und `temperature` werden aus den Live-Frames  dekodiert.
- `filter_raw` laesst sich ca. durch `raw / 4320 * 100` in Prozent umrechnen (Betriebsstundenzähler).
