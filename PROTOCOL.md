# AC500 BLE protocol notes

Stand: aus dem mitgeschnittenen `btsnoop_hci.log` abgeleitet.

## Pairing / bonding

- Reines Auslesen scheint auch ohne Bonding zu funktionieren.
- Schreibbefehle duerften Bonding voraussetzen.
- In den Mitschnitten wurde Pairing explizit gestartet und dann am Geraet mit der Bluetooth-Taste bestaetigt.
- Das lokale Skript hat deshalb jetzt einen `pair`-Befehl und einen globalen Schalter `--pair`.
- Die Mitschnitte zeigen zusaetzlich `EF03` als `PairingNotify` und diese Sequenz:

```text
write aa03a20003a8ee
warte auf EF03-notify aa03a20002a7ee
write aa03a20001a6ee
```

- Erst danach geht das Geraet in den normalen Steuer-/Statusbetrieb ueber.
- Die spaeteren Steuer-Mitschnitte zeigen keine zusaetzlichen Freischalt-Kommandos vor `fan`/`timer`; die beobachteten `ef01`-Writes selbst scheinen also korrekt zu sein.

## GATT

- Write: `0000ef01-0000-1000-8000-00805f9b34fb`
- Live data notify: `0000ef02-0000-1000-8000-00805f9b34fb`
- Ack/status helper notify: `0000ef03-0000-1000-8000-00805f9b34fb`
- Historic/raw notify: `0000ef04-0000-1000-8000-00805f9b34fb`

## Frame format

Alle beobachteten Schreibbefehle verwenden dieses Format:

```text
aa <len> <opcode> <arg1> <arg2> <checksum> ee
```

- `len` war in den beobachteten Schreibbefehlen immer `0x03`
- `checksum = (len + opcode + arg1 + arg2) & 0xff`

Beispiel:

```text
aa 03 02 00 01 06 ee
```

## Beobachtete Schreibbefehle

- `af 00 01`: Verbindungs-/Initialisierungsschritt
- `a2 00 03`: Live-Daten anfordern
- `a2 00 01`: Folgeabfrage nach `ef03`-Antwort, noch nicht vollstaendig eingeordnet
- `01 00 00`: Power off
- `01 00 01`: Power on
- `02 00 00`: Fan low
- `02 00 01`: Fan medium
- `02 00 02`: Fan high
- `02 00 03`: Fan turbo
- `03 00 00`: UV-C off
- `03 00 01`: UV-C on
- `04 00 00`: Timer off
- `04 00 02`: Timer 2h
- `04 00 04`: Timer 4h
- `04 00 08`: Timer 8h
- `05 00 00`: Auto off
- `05 00 01`: Auto on
- `06 00 00`: Night off
- `06 00 01`: Night on
- `08 00 00`: Buzzer off
- `08 00 01`: Buzzer on

Wichtig: Beim Wechsel von `auto` auf eine feste Fan-Stufe zeigen die Mitschnitte zuerst `05 00 00` und danach den eigentlichen `02 ...`-Befehl.
Der Buzzer-Befehl wurde durch Ausprobieren gefunden. Da `07` fehlt, wird hier eine weitere Funktion erwartet, es konnte jedoch keine festgestellt werden.

## Live status frame

Beispiel:

```text
aa0da02100008b000a00cb10e0045375ee
```

Aktuell belegt:

- Byte 4: Fan-Stufe (`0..3`)
- Byte 5: Timer (`0`, `2`, `4`, `8`)
- Byte 6: Flags fuer Power/UV/Timer/Buzzer/Auto/Night
- Bytes 8-9: Feinstaubwert als `raw / 10`, Beispiel `0x000a -> 1.0`
- Byte 10: Temperatur als `raw / 10`, Beispiel `0xcb -> 20.3`
- Bytes 12-13: Filter-Rohwert, beobachtet als etwa `raw / 4320 * 100`

Bit-Zuordnung 
- Bit 0: Power
- Bit 1: UV-C
- Bit 2: Timer aktiv
- Bit 3: Buzzer
- Bit 5: Auto
- Bit 6: Night
