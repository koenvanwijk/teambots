# Android-emulator in de TeamBots-sandbox

De Android-emulator is een **optie** van de bestaande project- of overzichtscomputer, niet een aparte sandbox. Standaard staat hij uit. Het gewone image `teambots-sandbox` blijft licht (browser, git, noVNC). Alleen als je de optie aanzet, wordt de container opnieuw gemaakt vanuit het variant-image `teambots-sandbox-android`, met `/dev/kvm` als dat op de host aanwezig is.

De emulator draait op het bestaande virtuele scherm van de bot (Xvfb). Je ziet hem dus in dezelfde noVNC-weergave en live preview als het scherm van TeamBots. `adb` is in de sandbox beschikbaar; de bot gebruikt het via een shellcommando (`run_command`).

## In- en uitschakelen

### In de UI

- **Projectcomputer:** open de projectinstellingen (als beheerder). Onder *Android-emulator* zet je de optie aan of uit, kies je eventueel softwaremodus, en stel je geheugen (MB) en CPU's in. Klik op *Opslaan*.
- **Overzichtsbot:** open de instellingen van de Overview-bot. Dezelfde velden staan onder het scherm van de bot.

Bij in- of uitschakelen (of het wijzigen van geheugen/CPU's) verschijnt de waarschuwing dat **de sandbox wordt herstart**. Open vensters en processen stoppen. De bestanden op het Docker-volume blijven bewaard.

### Via de API

Status van de host (KVM, image, Docker):

```http
GET /api/android-status
```

Voorbeeldantwoord:

```json
{
  "docker": true,
  "kvm_available": true,
  "android_image_present": true,
  "android_image": "teambots-sandbox-android:latest",
  "message": null
}
```

Als er geen KVM is, is `message` gelijk aan `geen KVM, emulator niet beschikbaar`.

Project (alleen projectbeheerder):

```http
PATCH /api/teams/{id}
Content-Type: application/json

{
  "android_enabled": true,
  "android_software": false,
  "android_memory_mb": 6144,
  "android_cpus": 4
}
```

Overzichtsbot (de eigenaar zelf):

```http
PATCH /api/home
Content-Type: application/json

{
  "android_enabled": true,
  "android_software": false,
  "android_memory_mb": 6144,
  "android_cpus": 4
}
```

Het antwoord bevat `sandbox_restarted` (true als een bestaande container is vervangen). Zonder KVM en zonder `android_software: true` antwoordt de API met HTTP 400 en de tekst `geen KVM, emulator niet beschikbaar`.

Standaardwaarden: `android_enabled` is uit, geheugen 6144 MB, 4 CPU's. Toegestaan: geheugen 2048–16384 MB, CPU's 1–8.

## Image bouwen

Het basisimage blijft lean. Het variant-image bevat Java, grafische bibliotheken en twee scripts. De SDK, de emulator en het system image (API 34, x86_64, `google_apis`) worden bij de eerste start in het gedeelde volume `tb-android-sdk` gezet, zodat meerdere projecten dezelfde SDK delen.

```bash
docker build -t teambots-sandbox:latest sandbox
docker build -t teambots-sandbox-android:latest -f sandbox/Dockerfile.android sandbox
```

Waarom deze tweedeling? Een alles-in-één-image zou 5–8 GB groot zijn en het gewone sandbox-image onnodig zwaar maken. Installatie-bij-gebruik in een volume houdt het basisimage klein en is robuust genoeg door een vastgepinde cmdline-tools-URL en een lock bij de eerste setup. Nadeel: de eerste keer dat je de optie aanzet is er netwerk nodig en duurt het even (enkele minuten). Een Hetzner Cloud-VPS heeft die ruimte meestal wel, maar meestal geen KVM (zie hieronder).

Omgevingsvariabelen (optioneel):

| Variabele | Standaard | Betekenis |
| --- | --- | --- |
| `TB_SANDBOX_ANDROID_IMAGE` | `teambots-sandbox-android:latest` | Variant-image |
| `TB_SANDBOX_ANDROID_SDK_VOLUME` | `tb-android-sdk` | Gedeeld volume voor de SDK |
| `TB_SANDBOX_ANDROID_MEMORY_MB` | `6144` | Standaardgeheugen als Android aan staat |
| `TB_SANDBOX_ANDROID_CPUS` | `4` | Standaard-CPU's als Android aan staat |
| `TB_SANDBOX_PREFIX` | `tb` | Prefix van containernamen (`tb-p3`, `tb-home-u1`). Gebruik een andere prefix voor een testdraai zodat productiesandboxes onaangeroerd blijven |

## Eisen

- Docker-sandbox (`TB_SANDBOX=docker`), niet de lokale ontwikkelbackend.
- **KVM** (`/dev/kvm` lees- en schrijfbaar voor de TeamBots-gebruiker). Zonder KVM start de emulator niet, tenzij je bewust *softwaremodus* aanzet (zeer traag, niet aanbevolen).
- Schijfruimte: reken op enkele gigabytes voor het variant-image plus het SDK-volume (cmdline-tools, emulator, system image API 34).
- RAM: de container krijgt standaard 6 GB; de emulator zelf gebruikt daar roughly 2 GB van. Zorg dat de host dat kan missen.

## Verbinden

- **Scherm:** zet bij de bot “Own screen and computer use” aan. Open het scherm via “Open scherm” of de live preview in de botinstellingen. De emulator verschijnt als venster op dat virtuele scherm (de bot die het scherm als eerste opent, krijgt het venster).
- **adb, in de sandbox:** de bot (of jij via een shellcommando) gebruikt `adb` binnen de container, bijvoorbeeld `adb devices` en `adb shell getprop sys.boot_completed`. Poorten van de emulator worden niet op de host gepubliceerd.
- Installeer alleen APK's van officiële bronnen, bijvoorbeeld `adb install app.apk` nadat het bestand in de workspace staat.

## Hetzner en KVM

Een gewone **Hetzner Cloud-VPS** (CX/CPX, de geplande host voor TeamBots) heeft doorgaans **geen KVM**. Nested virtualization staat daar niet aan. De emulator hoort daarom op de laptop (of een andere machine met KVM) te blijven, of je neemt een **Hetzner Dedicated Server** / Robot-machine waar KVM wel werkt.

Softwaremodus op een Cloud-VPS is technisch mogelijk als je die optie bewust aanzet, maar is te traag en te zwaar voor dagelijks gebruik. Zet de optie op zo'n VPS gewoon uit.
