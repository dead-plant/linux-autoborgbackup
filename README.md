## Dokumentation für das Borg-Backup-Skript
- **Dieser Code wurde teilweise mit Unterstützung von ChatGPT generiert.**

### Überblick

Dieses Skript ermöglicht ein automatisches, sicheres Backup von lokalen Verzeichnissen und optionalen ZFS-Pools mithilfe von [BorgBackup](https://www.borgbackup.org). Es integriert sich in bestehende Linux-Umgebungen (getestet z.B. auf Debian) und erledigt wichtige Aufgaben wie:

- **Backup-Erstellung** in frei konfigurierbaren Borg-Repositories, lokal oder remote.  
- **ZFS-Unterstützung**: Automatisches Erstellen von Snapshots, Read-Only-Mounten der Snapshots, Backup über Borg, anschließendes Unmount und Löschen der Snapshots.  
- **Lock-Mechanismus**: Das Skript wird nur einmal pro Zeitpunkt ausgeführt (ein vorhandenes Lock-File verhindert parallelen Start).  
- **Fehlerbehandlung** mit ausführlichem Logging (inkl. gesonderter Log-Datei pro Lauf).  
- **E-Mail-Benachrichtigungen** über Erfolg oder Fehlschläge (inklusive “error only”-Modus).  
- **Prune & Compact**: Automatisches Löschen älterer Backups nach bestimmten Aufbewahrungsregeln (täglich, wöchentlich, monatlich, jährlich) sowie optionales Kompaktieren der Borg-Repositories.  
- **Logfile-Aufräumfunktion**: Alte Logdateien können automatisch gelöscht werden, damit keine unendliche Ansammlung entsteht.  

### Funktionsweise

1. **Skript-Temp-Ordner:**  
   - Zu Beginn prüft das Skript (Funktion `check_script_tmp_dir()`), ob der temporäre Ordner bereits existiert.  
   - Wenn noch nicht vorhanden, wird er angelegt.  
   - Ist er nicht leer, bricht das Skript mit einer Fehlermeldung ab (z.B. wenn hier noch Reste eines vorherigen misslungenen Laufs liegen).

2. **Lock-Mechanismus:**  
   - Über `acquire_lock_or_exit()` legt das Skript eine Lock-Datei an, um parallele Ausführungen zu verhindern.  
   - Ist bereits eine Lock-Datei vorhanden, beendet es sich mit einem Fehler.  
   - Am Ende wird das Lock über `release_lock()` wieder gelöscht.

3. **Backup-Logik:**  
   - Konfigurationsvariablen (z.B. `BACKUP_DIRECTORIES`, `ZFS_POOLS`, `BORG_REPOSITORIES`) geben vor, was gesichert werden soll und wohin.  
   - Im Hauptteil des Skripts (`main()`) werden der Reihe nach folgende Schritte durchlaufen:  
     1. **ZFS-Snapshots** erstellen und mounten (falls in `ZFS_POOLS` Einträge vorhanden sind).  
     2. **Backup (borg create)**:  
        - Alles in `BACKUP_DIRECTORIES` **und** das gemountete ZFS-Verzeichnis (sofern vorhanden) wird in jedem Repository gesichert.  
     3. **Verifizierung (borg check)**: Überprüfung des Repositories (ggf. mit `--verify-data`).  
     4. **Prune (borg prune)**: Alte Backups werden gemäß den eingestellten Aufbewahrungsregeln (daily, weekly, monthly, yearly) entfernt.  
     5. **Optionales (borg compact)**: Die Repositories können komprimiert / aufgeräumt werden.  
   - Abschließend werden **ZFS-Snapshots** ungemountet und zerstört.

4. **Logging und Fehlerbehandlung:**  
   - Das Skript schreibt zu jedem Lauf eine neue Logdatei (gespeichert in `LOG_DIR`).  
   - **Erfolgreiche** Kommandos werden in `logger.debug()` mit entsprechenden Meldungen vermerkt.  
   - Bei **Fehlermeldungen** wird `logger.error()` genutzt, und das Skript sammelt die Fehlgründe in einer Liste (`backup_fail_reasons`).  
   - Eine Zusammenfassung (Erfolg/Fehlschlag, Dauer des Backups, Fehlgründe) wird am Ende ausgegeben und ggf. per E-Mail verschickt.

5. **E-Mail-Versand:**  
   - Nach Abschluss aller Vorgänge versendet das Skript (außer im “error only”-Modus bei Erfolg) eine E-Mail mit der Zusammenfassung und dem **vollständigen** Log (inkl. Leerzeilen zur besseren Lesbarkeit).  
   - Alle SMTP-Einstellungen werden in den Konfigurationsvariablen festgelegt.  

6. **Logdateien aufräumen:**  
   - Mithilfe von `garbage_collect_logs()` entfernt das Skript alte Logfiles, sodass nur die zuletzt festgelegte Anzahl (`LOG_GARBAGE_KEEP`) übrig bleibt.  

7. **Nur Directories / Nur ZFS / Beides**  
   - Falls `BACKUP_DIRECTORIES` leer ist, werden **nur** ZFS-Pools gesichert.  
   - Falls `ZFS_POOLS` leer ist, werden **nur** Verzeichnisse gesichert.  
   - Sind beide Listen nicht leer, wird beides gesichert.  
   - Sind **beide** leer, bricht das Skript ab.  

---

### Konfiguration

1. **Allgemeine Variablen** (oben im Skript):  
   - `SCRIPT_NAME`: Basisname für Lockfile und Logfiles.  
   - `SCRIPT_TMP_DIR`: Pfad zum temporären Skript-Ordner (enthalten auch Lockfile, ZFS-Mounts etc.).  
   - `LOG_DIR`: Wohin die Logfiles geschrieben werden.  
   - `LOG_GARBAGE_KEEP`: Wie viele Logfiles behalten werden sollen.  
   - `BACKUP_NAME_PREFIX`: Prefix für die Borg-Archive (z.B. “server-backup”).  

2. **E-Mail-Einstellungen**:  
   - `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USE_TLS`, `SMTP_USERNAME`, `SMTP_PASSWORD`: SMTP-Zugangsdaten.  
   - `EMAIL_FROM_ADDRESS`, `EMAIL_FROM_NAME`: Absender-Informationen.  
   - `EMAIL_RECIPIENTS`: Liste der Empfängeradressen.  
   - `EMAIL_ERROR_ONLY_MODE`: Wenn `True`, wird **nur** bei Fehlern eine E-Mail verschickt.  

3. **Borg-Repositories** (`BORG_REPOSITORIES`):  
   - Liste von Dicts, z.B.  
     ```python
     BORG_REPOSITORIES = [
       {
         "repo_url": "ssh://user@host:port/./borgrepo",
         "encryption_mode": "repokey",
         "passphrase": "top_secret_passphrase",
         "ssh_key": "/pfad/zu/key"
       }
     ]
     ```  
   - `ssh_key` kann `None` sein oder auf eine Datei zeigen.  

4. **Zu sichernde Verzeichnisse** (`BACKUP_DIRECTORIES`):  
   - Einfach eine Liste mit den Pfaden, z.B. `["/root/backupfolder", "/etc"]`.  

5. **ZFS-Pools** (`ZFS_POOLS`):  
   - Liste der zu sichernden Pools (ohne `-r` / **nicht** rekursiv). Beispielsweise `["rpool/data", "myzpool"]`.  

6. **Prune-Einstellungen**:  
   - `PRUNE_KEEP_DAILY`, `PRUNE_KEEP_WEEKLY`, `PRUNE_KEEP_MONTHLY`, `PRUNE_KEEP_YEARLY`.  
   - `ENABLE_BORG_COMPACT` legt fest, ob nach dem Prune ein `borg compact` erfolgt.  

7. **Check-Einstellungen**:  
   - `CHECK_WITH_VERIFY_DATA = True`: Führt bei `borg check` auch eine Überprüfung der Datenblöcke durch.  

---

### Installation und Einrichtung

1. **Voraussetzungen**:  
   - Python 3 (inkl. benötigter Module wie `subprocess`, `logging`, `smtplib` usw. – standardmäßig vorhanden).  
   - [BorgBackup](https://www.borgbackup.org/) installiert.  
   - (Optional) ZFS-Unterstützung installiert und eingerichtet, wenn ZFS-Pools gesichert werden sollen.

2. **Datei kopieren**:  
   - Skriptdatei (z.B. `automated_borg_backup.py`) in ein geeignetes Verzeichnis legen (z.B. `/usr/local/bin`).  
   - Skript ausführbar machen:  
     ```bash
     chmod +x automated_borg_backup.py
     ```

3. **Konfiguration anpassen**:  
   - Oben im Skript die Variablen für SMTP, Borg-Repositories, Verzeichnisse (`BACKUP_DIRECTORIES`) und ZFS-Pools (`ZFS_POOLS`) anpassen.  
   - Ggf. `SCRIPT_TMP_DIR` und `LOG_DIR` ändern.  

4. **SSH-Schlüssel / Passphrase**:  
   - Wenn ein Repository per SSH erreichbar ist, in `BORG_REPOSITORIES` den korrekten Pfad zum SSH-Key angeben und ggf. die Passphrase für das Repo eintragen.  
   - Falls kein SSH-Schlüssel oder keine Passphrase benötigt wird, die entsprechenden Werte auf `None` oder einen leeren String setzen.

5. **Erster Testlauf**:  
   - Das Skript als Root oder mit ausreichenden Berechtigungen starten:  
     ```bash
     ./automated_borg_backup.py
     ```  
   - In `/var/log/automated_borg_backup/` (oder dem in `LOG_DIR` angegebenen Pfad) entsteht eine Logdatei mit Informationen zum Ablauf.  
   - Eventuelle Fehler werden im Logfile dokumentiert.  

6. **Automatisierung per Cron** (empfohlen):  
   - Einen Crontab-Eintrag anlegen, z.B.  
     ```bash
     crontab -e
     ```
   - Und dort z.B. einmal täglich um 04:00 Uhr ausführen lassen:  
     ```
     0 4 * * * /usr/local/bin/automated_borg_backup.py
     ```
   - (Je nach Installation ist evtl. der vollständige Pfad zu Python nötig oder ein Shebang oben im Skript.)

---

### Verwendung

1. **Manueller Start**:  
   - Ausführen:  
     ```bash
     ./automated_borg_backup.py
     ```  
   - Das Skript prüft, ob ein Lock besteht (falls ja, Abbruch), erstellt ein Lock-File und startet die Sicherungsprozedur.  

2. **Ergebnis überprüfen**:  
   - Nach Beendigung findet sich im Logverzeichnis (`LOG_DIR`) eine neue Logdatei.  
   - Ist ein E-Mail-Versand konfiguriert, erhält man (je nach Einstellung) eine E-Mail mit Zusammenfassung und komplettem Log.  

3. **Spezielle Fehlerfälle**:  
   - **Temp-Ordner nicht leer**: Skript bricht ab, damit keine alten Daten überschrieben werden.  
   - **Lockfile vorhanden**: Skript bricht ab, um parallele Ausführung zu vermeiden.  
   - **ZFS-Snapshot-Fehler**: Wird im Log protokolliert, das Skript versucht, die anderen Pools weiter zu sichern.  
   - **Borg-Fehler** (z.B. kein Zugriff auf Repository): Wird protokolliert und in die `backup_fail_reasons` aufgenommen.  

4. **Nachträgliche Anpassungen**:  
   - Bei Änderungen an `BORG_REPOSITORIES`, `BACKUP_DIRECTORIES` oder `ZFS_POOLS` einfach das Skript erneut ausführen.  
   - Achten Sie darauf, dass sich das Lockfile und die ZFS-Snapshots nicht überschneiden (Lockordner leer halten usw.).  

---

### Fazit

Mit diesem Skript lassen sich lokale Verzeichnisse und/oder ZFS-Pools automatisiert über BorgBackup sichern. Dank ausführlicher Logging- und E-Mail-Benachrichtigungen hat man stets Überblick über **erfolgreiche** oder **fehlgeschlagene** Backups. Die ZFS-Funktionen sorgen dafür, dass konsistente Snapshots erzeugt und ins Borg-Archiv übernommen werden, bevor sie wieder aufgeräumt werden.  

Bei ordnungsgemäßer Konfiguration und Einbindung in `cron` erhält man so eine zuverlässige Backup-Lösung mit minimalem Administrationsaufwand.