# ebus-pi

Vervangt de **LoxBerry met ebusd-plugin** op de Raspberry Pi 3B (192.168.255.3) door een kale
Raspberry Pi OS Lite met:

- **ebusd** (GitHub-release van john30/ebusd), met dezelfde opties en ketelconfig als nu, en MQTT naar Mosquitto op 10.10.30.182;
- **dezelfde drie adressen als de LoxBerry-plugin**, zodat er in Loxone Config niets hoeft te veranderen;
- een **Modbus TCP-server** (poort 502) voor Loxone, in beide richtingen;
- een **beheerwebsite** voor status, waarden, SetMode, wifi, ebusd-opties, logboek, updates en herstart;
- **wifi met een noodhotspot** `ebus-pi` en captive portal (dezelfde aanpak als het KHZS-timingkastje);
- **bewaking**: watchdog, controles op de ketel, gezondheid op MQTT, logboek in RAM, en updates met automatisch terugdraaien.

Alles werkt **zonder Flux**. MQTT is enkel uitvoer, en de tunnel naar Flux is optioneel. Het lokale beheer op
`http://192.168.255.3/` werkt altijd.

## Wat de LoxBerry-plugin deed, en wat hier gebeurt

| Loxone-object | Adres | Plugin (LoxBerry) | ebus-pi |
|---|---|---|---|
| VirtualOut **ebusd SetMode** (3 commando's, herhaling 60 s) | `POST …/api/ebusd/setmode.php` met `{ "flowtempdesired": <v.1> }`, `hwctempdesired`, `hwcflowtempdesired` | onthoudt de velden en schrijft bij **elke** POST `SetMode` | onthoudt de velden (ook na herstart) en schrijft **enkel bij een wijziging**, plus een vaste herhaling om de 60 s. Er wordt pas geschreven als de drie temperaturen van Loxone bekend zijn |
| VirtualOut **ebusd hex** (circulatiepomp) | `POST …/api/ebusd/hex.php` met `{"hex":"08B512020064"}` / `…0000` | `ebusctl hex …` (ongefilterd naar de shell) | `hex` naar ebusd, enkel geldige hex |
| VirtualHttpIn **ebud read** (poll 10 s) | `GET …/api/ebusd/read.php?circuit=bai&update[]=Statenumber&update[]=StorageTemp&update[]=WaterPressure&update[]=FlowTemp` | `ebusctl read` per waarde (ebusd-geheugen, tot 5 min oud) | antwoord uit het geheugen (≤ 30 s oud), anders een verse lezing. **Zelfde JSON**: `{"Statenumber":"7","StorageTemp":"34.00","WaterPressure":"1.904","FlowTemp":"26.19"}` |

Het pad is altijd `/admin/plugins/ebusdplugin/api/ebusd/`, met HTTP Basic auth (gebruiker `ebusberry`, wachtwoord zoals
in de Loxone-URL's, enkel lokaal bewaard als hash).

**SetMode:** de GitLab-versie van de plugin schrijft een zelf gedefinieerd `SetModeOverride`. Op de echte LoxBerry
bestaat dat bericht niet (`ERR: element not found`). Wat er wel op de bus staat, is het gewone bericht `SetMode`
uit de ketelconfig (`vaillant/08.bai.csv`, `uw,bai,SetMode,…,08,b510,00`). De laatste waarde was
`auto;30.0;45.0;65;0;0;0;0;0;0`, en de ketel nam die over (`FlowTempDesired` = 30.00). ebus-pi schrijft daarom
`SetMode` met dezelfde tien velden, als gehele getallen zoals de plugin (`(int)` in PHP), en `hcmode` als naam
(`auto`). De variant met `SetModeOverride` kan nog via de config (`setmode.message`, `define`, `trailing_semicolon`).

## Verbeteringen tegenover de LoxBerry

- **Verse waarden.** `Status01` (aanvoer, retour, buiten, warm water, boiler, pomp), `WaterPressure` en
  `Statenumber` worden om de 10 s gelezen. ebusd zet ze daarbij zelf op MQTT (`ebusd/bai/Status01`).
  **Afspraak met Flux:** de Pi doet deze lezing. Flux hoeft `ebusd/bai/Status01/get` niet meer te sturen en luistert enkel.
- **Minder schrijfwerk op de bus:** SetMode gaat 1× per minuut naar de bus in plaats van 3× (de drie herhalingen van Loxone).
- **Veiliger.**
  - Geen shell meer tussen het web en ebusd.
  - ebusd luistert enkel op localhost (`--localhost`). Nu kan iedereen op het LAN zonder wachtwoord naar de ketel schrijven.
  - Modbus is enkel open voor de Miniserver.
- **SD-kaart sparen.** Het logboek staat in RAM, ebusd logt naar het journal, en een alleen-lezen systeem is optioneel (Systeem → SD-kaart).
- **Zelfherstel.**
  - Er is een hardware-watchdog.
  - ebusd herstart vanzelf bij een crash, zonder bussignaal of als de adapter terug ingeplugd wordt (udev). De watchdog doet maximaal 4 herstarts per uur.
  - De systemd-watchdog bewaakt de dienst zelf.
- **Controles,** gemeld in het beheer en op MQTT `ebusd/ebuspi/alarm`:
  - ketelstoring (`currenterror`);
  - waterdruk (< 1,0 bar waarschuwing, < 0,8 bar alarm, > 2,8 bar);
  - een defecte sensor;
  - de ketel die het gevraagde aanvoersetpoint negeert;
  - geen bussignaal of ebusd weg;
  - **Loxone krijgt 401** (gebruiker of wachtwoord klopt niet; de gebruikersnaam wordt getoond, het wachtwoord nooit);
  - **Loxone komt niet meer langs:** al 5 min geen geslaagd verzoek op de plugin-adressen (`checks.loxone_silence_s`, 0 = uit).
- **Loxone-bewaking:** per bron-IP het laatste geslaagde verzoek, het aantal 401's en de gebruikersnaam. Dat staat in het beheer (Status → Loxone) en in `/api/status` (`loxone`, `loxone_last_ok_s`), dus ook via Flux.
- **Ketel niet gevonden na een start:** soms vindt de eerste ebusd-scan de ketel niet ("Status01 not found"). ebus-pi stuurt dan zelf `scan 08`, hoogstens om de 10 min.
- **Oude LoxBerry-adressen:** browsers onthouden de doorverwijzing van de LoxBerry naar `/admin/system/index.cgi`. Daar, en op elk ander `/admin/….cgi`, toont ebus-pi gewoon het beheer.
- **Gezondheid op MQTT:** `ebusd/ebuspi/status` (retained JSON, `{"state":"offline"}` als testament).
- **Actueel OS:** Raspberry Pi OS 64-bit met automatische beveiligingsupdates, in plaats van Raspbian 11.

## Modbus TCP (optioneel, naast HTTP)

Poort 502, unit-ID 1, enkel voor de Miniserver: `192.168.255.2` en `10.10.10.164`, het adres waarvan Loxone de HTTP-verzoeken echt doet (instelbaar). Registers zijn signed 16-bit. `-32768` betekent
"geen waarde": zet in Loxone het geldige bereik zodat je die waarde negeert.

**Inputregisters (functie 4, alleen lezen)**

| Adres | Waarde | Schaal |
|---|---|---|
| 0 | aanvoertemperatuur (Status01) | ×10 °C |
| 1 | retourtemperatuur | ×10 °C |
| 2 | buitentemperatuur (ketelsensor, vaak leeg) | ×10 °C |
| 3 | warmwatertemperatuur | ×10 °C |
| 4 | boilertemperatuur | ×10 °C |
| 5 | pomp draait (0/1) | |
| 6 | waterdruk | ×1000 bar |
| 7 | statusnummer (Statenumber) | |
| 8 | ebusd bereikbaar (0/1) | |
| 9 | bussignaal (0/1) | |
| 10 | leeftijd van Status01 | s |
| 11 | laatste SetMode gelukt (0/1) | |
| 12 | hartslag (loopt op per seconde) | |
| 13 | circulatiepomp, laatste opdracht (0/1) | |

**Holdingregisters (functie 3 lezen, 6/16 schrijven)**

| Adres | Waarde | Schaal |
|---|---|---|
| 0 | flowtempdesired | ×10 °C |
| 1 | hwctempdesired | ×10 °C |
| 2 | hwcflowtempdesired | ×10 °C |
| 3 | hcmode (0 auto, 1 off, 2 water) | |
| 4–6 | disablehc, disablehwctapping, disablehwcload | 0/1 |
| 7 | circulatiepomp (stuurt dezelfde hex als de Loxone-uitgang) | 0/1 |

Schrijven via Modbus komt in dezelfde SetMode-toestand terecht als via HTTP, dus ze bijten elkaar niet.
Steek je de LoxBerry-kaart terug, dan valt enkel de Modbus-verbinding weg en werkt HTTP gewoon verder.

**Push zonder pollen (optioneel):** met `loxone_udp.enabled` stuurt de Pi `FlowTemp=41.73` (enz.) naar een
*Virtual UDP Input* van de Miniserver, zodra een waarde wijzigt.

## Demo op je pc

```bash
python tools/demo_reload.py --port 8080
```

Daarna open je http://127.0.0.1:8080/ (gebruiker `demo`, wachtwoord `demo`). Er hangt een nagebootste ketel en
eBUS-adapter aan: de aanvoer volgt het SetMode-setpoint, de brander slaat aan en af, en de circulatiepomp volgt `hex`.
De Modbus-demo luistert op poort 1502. De demo herstart vanzelf bij elke codewijziging.
Met `--live 192.168.255.3` leest hij de echte ebusd, **alleen lezend**: SetMode en hex worden dan nooit verstuurd.

## Tests

```bash
python -m pytest -q
```

De tests draaien met een nagebootste ebusd (`ebuspi/fake_ebusd.py`). Die antwoordt zoals de echte ebusd 26.1 op de LoxBerry.

## Overstapplan

Er is maar één Pi. De nieuwe SD-kaart **is** dus de overstap, en **terugdraaien = de oude kaart terugsteken**.
Daarom halen we alles vooraf op, terwijl de LoxBerry nog draait.

1. **Vooraf, op je pc (Git Bash), terwijl de LoxBerry draait:**
   ```bash
   bash tools/collect-loxberry.sh loxberry@192.168.255.3
   ```
   - Je typt zelf het SSH-wachtwoord van de LoxBerry. Daar wordt niets gewijzigd.
   - Het resultaat is de map `loxberry-overname/` met de ebusd-config en -opties, de huidige SetMode en de pluginlijst en -config.
   - Die map bevat het MQTT-wachtwoord: ze staat in `.gitignore` en hoort niet in git.
2. **SD-kaart maken** met Raspberry Pi Imager:
   - Kies **Raspberry Pi OS Lite (64-bit)** voor de Pi 3B.
   - Vul bij "OS aanpassen" in: gebruikersnaam en wachtwoord, SSH aan, en de **wifi** (zelfde netwerk als de LoxBerry, met je eigen sleutel).
   - Kopieer daarna deze repo en de map `loxberry-overname` naar de bootpartitie (`bootfs`).
3. **Oude kaart eruit, nieuwe erin.** De LoxBerry-kaart zorgvuldig bewaren.
   - Het is dezelfde Pi met hetzelfde wifi-MAC-adres, dus de router geeft (via de DHCP-reservatie) opnieuw **192.168.255.3**.
   - Lukt de wifi niet, dan start na 1 minuut de hotspot **ebus-pi**. Je gsm opent dan het beheer, waar je de wifi kan instellen.
4. **Installeren** (ssh naar de Pi):
   ```bash
   sudo bash /boot/firmware/ebus-pi/install.sh --from-dir /boot/firmware/loxberry-overname
   ```
   Het script vraagt het wachtwoord voor `ebusberry` (hetzelfde als in de Loxone-URL's) en een hotspotwachtwoord.
   Het MQTT-wachtwoord neemt het over uit de oude opties.
5. **Nakijken** op `http://192.168.255.3/`:
   - **Status:** ebusd ok, bussignaal `acquired`, ketel `BAI00` gevonden.
   - **Waarden:** aanvoer, boiler en druk in lijn met wat Loxone en Flux eerder toonden.
   - **SetMode:** binnen een minuut "geschreven" met de setpoints van Loxone. De ketel neemt ze over (geen melding "setmode").
   - **In Loxone:** de "ebud read"-ingangen lopen door, de Online-status is groen, en de circulatiepomp werkt.
   - **In Flux:** `ebusd/bai/…` komt nog binnen op MQTT.
6. **Terug nodig?** Stroom eraf, de LoxBerry-kaart terugsteken, stroom erop. Er is niets aan Loxone veranderd.
7. **Pas later, en enkel als je wil:** Modbus of UDP-push in Loxone toevoegen, naast de bestaande ingangen.

## Wat er verder nog op de LoxBerry draait (moet mee)

Wat in het Loxone-project naar de LoxBerry (192.168.255.3) verwijst:

| Loxone-object | Wat | Na de overstap |
|---|---|---|
| VirtualHttpIn "ebud read", VirtualOut "ebusd SetMode" en "ebusd hex" | ebusd-plugin | **ebus-pi** (deze repo) |
| **VirtualUdpIn "Netatmo"** (bron 192.168.255.3, poort 5005): zithoek-temperatuur, CO2, vochtigheid, geluid van station "Anja en Björn" | LoxBerry-**Netatmo-plugin**, stuurt UDP naar de Miniserver | **valt weg, bewust** (Netatmo-waarden zijn niet meer nodig in Loxone). Verwijder na de overstap de VirtualUdpIn "Netatmo" uit Loxone Config, of laat hem staan: hij krijgt gewoon niets meer binnen |
| Miniserver-gebruiker "Loxberry" | gebruikt door plugins die waarden in Loxone zetten | geen van de plugins op .3 doet dat nog (zie hieronder) |

"Loxberry MQTT-Gateway" en de https2http-URL (Weatherflow) in het project wijzen naar **192.168.255.4**: dat is een ander toestel en valt buiten deze overstap.

Pluginlijst van de LoxBerry op .3 (nagekeken op 12/11/2026):

| Plugin | Versie | Na de overstap |
|---|---|---|
| eBUSd plugin | 1.1.4 | **ebus-pi** |
| Netatmo-Weather | 2.0.7 | valt bewust weg (zie hierboven) |
| Marstek Venus E | 1.1.34 | valt weg zonder gevolg: er zijn geen toestellen ingesteld en MQTT staat uit. De batterijwaarden in Loxone komen via de MQTT-Gateway op .4, en FLUX stuurt de batterijen rechtstreeks. |
| https2http | 2023.08.10 | valt weg zonder gevolg: Loxone gebruikt de https2http op .4, niet die op .3 |

In de crontab van de gebruiker `loxberry` staat nog `ebus-fastpoll.sh` (bij het opstarten en elke 15 min), maar dat script bestaat niet meer. ebus-pi doet dat snel uitlezen zelf: Status01, WaterPressure en Statenumber elke 10 s.

## Beheer via Flux (optioneel)

Dit werkt zoals bij de timingkastjes. De Pi houdt zelf een uitgaande long-poll naar Flux open, dus er moet geen poort open op de Pi.

- `GET {url}/next?device=ebus-pi` met `Authorization: Bearer <token>`. Antwoord `204` = niets te doen, of `{"id","method","path","headers","body_b64"}`.
- `POST {url}/reply` met `{"id","status","headers","body_b64"}`.

Het beheerwachtwoord van de Pi blijft gelden (de `Authorization` van de gebruiker gaat mee). De Loxone-adressen
gaan nooit via de tunnel. De Flux-kant moet nog gebouwd worden. Valt Flux weg, dan merkt de Pi daar niets van.

## Updates

- **ebus-pi** (Updates in het beheer):
  - Bron: GitHub-releases (`DinXke/ebus-pi`, token enkel nodig voor een privé repo) of een adres. Dat adres kan ook op Flux staan, maar dan als *bron*, nooit als vereiste.
  - Een release maak je met `python tools/build_release.py`, en upload dan `dist/ebus-pi-<versie>.tar.gz` en `.sha256` als assets van release `v<versie>`.
  - Installeren gaat zo: sha256 nakijken, zelftest, `/opt/ebuspi/current` omzetten, herstarten. Start de nieuwe versie niet binnen 60 s, dan komt de vorige terug.
  - Automatisch installeren staat standaard uit. Als je het aanzet, gebeurt het enkel als het rustig is (geen SetMode-wijziging, geen melding).
- **ebusd:** nooit automatisch. Gebruik de knop "ebusd bijwerken" in het beheer, of `sudo ebuspi-helper upgrade-ebusd`.
- **Debian-beveiligingsupdates:** automatisch, via `unattended-upgrades`.
- **Code bijwerken met de hand:** `sudo bash install.sh --update`.

## Paden op de Pi

| Wat | Pad |
|---|---|
| code | `/opt/ebuspi/current` (symlink naar `/opt/ebuspi/releases/<versie>`) |
| instellingen (600, met wachtwoord-hash) | `/etc/ebuspi/config.json` |
| MQTT-wachtwoord van ebusd | `/etc/ebuspi/ebusd.env` |
| ebusd-opties | `/etc/default/ebusd` (zonder wachtwoord) |
| ketelconfig | `/etc/ebusd/` |
| SetMode-toestand | `/var/lib/ebuspi/setmode.json` |
| diensten | `ebusd` (drop-in `ebusd.service.d/ebuspi.conf`) en `ebuspi` |
| root-acties | `/usr/local/sbin/ebuspi-helper` (vaste lijst, via sudo) |

Logboek bekijken: `journalctl -u ebuspi -f` en `journalctl -u ebusd -f`.
