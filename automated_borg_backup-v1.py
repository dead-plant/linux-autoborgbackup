#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Automatisches Backup-Skript für Debian (und andere Linux-basierte Systeme) mit BorgBackup.
Features:
- Exklusives Ausführen (Lock-Mechanismus)
- Logging (jedes Mal eine neue Log-Datei)
- Error Handling
- E-Mail-Benachrichtigung (mit optionalem "error only"-Modus)
- Mehrere Borg-Repositories (inkl. separater SSH-Key pro Repo möglich)
- Ein einzelnes Array für zu sichernde Verzeichnisse (gilt für alle Repos)
- Backup-Erstellung, Verifizierung (borg check), Prune, optional Compact
- Garbage Collect für Logdateien
- Detaillierte E-Mail-Zusammenfassung (optional nur bei Fehlern)
- **Neu**:
  1) Skript-Temp-Ordner mit Lock-Datei
  2) ZFS-Pools backupen (Snapshot + Mount)
  3) Nur ZFS / nur Directories / beides möglich
  4) Umfangreiches Logging und Error-Handling
"""

import os
import shutil
import sys
import logging
import datetime
import smtplib
import glob
import subprocess
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import email.utils


# =============================================================================
# ========================== KONFIGURATIONSVARIABLEN ==========================
# =============================================================================

# 1) Allgemeine Einstellungen
SCRIPT_NAME = "automated_borg_backup"   # Dient u.a. für den Log- und Lockfile-Namen
# Neuer Pfad für unseren Skript-Temp-Ordner (enthält Lockfile, ggf. ZFS-Mounts usw.)
SCRIPT_TMP_DIR = "/tmp/my_backup_tempdir"

LOCKFILE_PATH = os.path.join(SCRIPT_TMP_DIR, f".lock")

# Logging-Einstellungen
LOG_DIR = "/var/log/automated_borg_backup"  # Ordner, in dem die Logfiles erstellt werden
LOG_GARBAGE_KEEP = 5  # Anzahl der Logdateien, die behalten werden sollen. 0 = unendlich behalten

# Eindeutiger Name für die Backups, z.B. "meinserver"
BACKUP_NAME_PREFIX = "linux-backup"

# 2) E-Mail-Einstellungen
SMTP_SERVER = "mail.myserver.de"
SMTP_PORT = 587
SMTP_USE_TLS = True
SMTP_USERNAME = "from@example.com"
SMTP_PASSWORD = "SecureSMTPPassword"
EMAIL_FROM_ADDRESS = "from@example.com"
EMAIL_FROM_NAME = "Backup Script"
EMAIL_RECIPIENTS = [
    "to@example.com",
    "to2@example.com"
]

# "Error only"-Modus für E-Mail-Benachrichtigungen:
# Wenn True, werden nur bei einem Fehler E-Mails verschickt.
# Wenn False, wird immer eine E-Mail verschickt (unabhängig vom Ergebnis).
EMAIL_ERROR_ONLY_MODE = False

# 3) Borg-Repositories
# "ssh_key" kann None oder ein Pfad (z.B. "/root/.ssh/id_ed25519") sein.
BORG_REPOSITORIES = [
    {
        "repo_url": "ssh://user@borgserver:23/./directory",
        "encryption_mode": "repokey",
        "passphrase": "top_secret_passphrase",
        "ssh_key": "/root/.ssh/mysshkey"
    }
]

# 4) Zu sichernde Verzeichnisse (für alle Repositories gleich)
BACKUP_DIRECTORIES = [
    "/root/backupfolder"
]

# 5) Liste der zu sichernden ZFS-Pools (nicht rekursiv!)
# Beispiel: ["rpool/data", "myzpool"]
ZFS_POOLS = [

]

# 6) Backup-Prune-Einstellungen
# Siehe borg prune --help für Details
PRUNE_KEEP_DAILY = 7
PRUNE_KEEP_WEEKLY = 4
PRUNE_KEEP_MONTHLY = 6
PRUNE_KEEP_YEARLY = 2

# 7) Soll nach dem Prune ein "borg compact" ausgeführt werden?
ENABLE_BORG_COMPACT = True

# 8) Sollen in der Überprüfung (borg check) die Datenblöcke verifiziert werden?
# (Parameter: --verify-data)
CHECK_WITH_VERIFY_DATA = True


# =============================================================================
# =========================== GLOBALE VARIABLEN ===============================
# =============================================================================

logger = None
backup_success = True   # Wird auf False gesetzt, sobald ein Fehler auftritt
backup_fail_reasons = []  # Liste von Fehlerursachen (z.B. "Prune failed")
start_time = datetime.datetime.now()


# =============================================================================
# ============================= HILFSFUNKTIONEN ===============================
# =============================================================================

def setup_logging():
    """
    Initialisiert das Logging-System. Für jeden Skriptlauf wird eine neue Logdatei erstellt.
    """
    global logger

    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR, exist_ok=True)

    timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H-%M-%S")
    logfile_name = f"{SCRIPT_NAME}_{timestamp_str}.log"
    logfile_path = os.path.join(LOG_DIR, logfile_name)

    logger = logging.getLogger(SCRIPT_NAME)
    logger.setLevel(logging.DEBUG)

    # Formatter
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

    # File Handler
    fh = logging.FileHandler(logfile_path)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # Optional: Stream Handler (Konsole)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    logger.info("==============================================")
    logger.info("Backup-Skript startet...")
    logger.info("Log-Datei: %s", logfile_path)

    return logfile_path


def check_script_tmp_dir():
    """
    Prüft, ob der Skript-Temp-Ordner existiert, legt ihn ggf. an und
    wirft einen Fehler, falls er nicht leer ist.
    """
    if not os.path.exists(SCRIPT_TMP_DIR):
        try:
            os.makedirs(SCRIPT_TMP_DIR, exist_ok=False)
            logger.debug(f"SCRIPT_TMP_DIR erstellt: {SCRIPT_TMP_DIR}")
        except Exception as e:
            logger.error(f"Konnte den Skript-Temp-Ordner nicht erstellen: {e}")
            sys.exit(1)
    # Wenn Verzeichnis existiert, prüfen ob leer
    if os.listdir(SCRIPT_TMP_DIR):
        # Wenn nicht leer -> Fehler
        logger.error(f"Der Ordner {SCRIPT_TMP_DIR} ist nicht leer. Abbruch.")
        sys.exit(1)


def acquire_lock_or_exit(logfile_path):
    """
    Sorgt dafür, dass das Skript nur einmal zur gleichen Zeit ausgeführt wird.
    Erzeugt eine Lock-Datei und beendet sich, wenn es die Lock-Datei bereits gibt.
    """
    if os.path.exists(LOCKFILE_PATH):
        # Lockfile existiert bereits
        logger.error("Lockfile existiert bereits. Ein weiteres Ausführen ist nicht erlaubt.")
        send_email(
            "[BorgBackup] FAILED: Backup Already Running",
            f"The backup script did not start because a lockfile already exists at {LOCKFILE_PATH}.",
            logfile_path
        )
        sys.exit(1)
    else:
        # Lockfile erstellen
        try:
            with open(LOCKFILE_PATH, 'w') as lockfile:
                lockfile.write(str(os.getpid()))
            logger.debug(f"Lockfile erstellt: {LOCKFILE_PATH}")
        except Exception as e:
            logger.error(f"Lockfile konnte nicht erstellt werden: {e}")
            send_email(
                "[BorgBackup] FAILED: Unable to create lock file",
                f"The backup script did not start because the lockfile cant be created at {LOCKFILE_PATH}.",
                logfile_path
            )
            sys.exit(1)


def release_lock():
    """
    Entfernt das Lockfile am Ende des Skripts.
    """
    if os.path.exists(LOCKFILE_PATH):
        try:
            os.remove(LOCKFILE_PATH)
            logger.debug("Lockfile entfernt.")
        except Exception as e:
            logger.error(f"Lockfile konnte nicht entfernt werden: {e}")


def send_email(subject, body_text, log_file_path):
    """
    Versendet eine E-Mail an die konfigurierten Empfänger.
    Der Mail-Body enthält eine Kurzzusammenfassung und das gesamte Log.
    Nach jedem Log-Eintrag wird eine Leerzeile eingefügt.
    """
    # MIME Konstruktion
    msg = MIMEMultipart()
    msg["From"] = f"{EMAIL_FROM_NAME} <{EMAIL_FROM_ADDRESS}>"
    msg["To"] = ", ".join(EMAIL_RECIPIENTS)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)

    # Logfile-Inhalt auslesen
    try:
        with open(log_file_path, "r") as lf:
            log_content = lf.read()
    except Exception as e:
        log_content = f"Fehler beim Lesen des Logfiles: {e}"

    # Nach jedem Zeilenumbruch eine Leerzeile einfügen
    log_content = log_content.replace("\n", "\n\n")

    # Body aufbauen
    body = body_text
    body += "\n\n--- Vollständiges Log ---\n\n"
    body += log_content

    msg.attach(MIMEText(body, "plain", "utf-8"))

    # SMTP-Verbindung herstellen und Mail senden
    try:
        if SMTP_USE_TLS:
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
            server.starttls()
        else:
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)

        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(EMAIL_FROM_ADDRESS, EMAIL_RECIPIENTS, msg.as_string())
        server.quit()
        logger.info("E-Mail wurde erfolgreich versendet.")
    except Exception as e:
        logger.error(f"Fehler beim Versenden der E-Mail: {e}")


def run_command(command, passphrase=None, ssh_key=None):
    """
    Führt einen Shell-Befehl aus und gibt den Rückgabecode, stdout und stderr zurück.
    Setzt ggf. BORG_PASSPHRASE, falls passphrase != None.
    Setzt ggf. BORG_RSH="ssh -i <ssh_key>", falls ssh_key != None.
    """
    env = os.environ.copy()

    if passphrase is not None:
        env["BORG_PASSPHRASE"] = passphrase

    if ssh_key is not None:
        env["BORG_RSH"] = f"ssh -i {ssh_key}"
    else:
        # Falls kein eigener Key spezifiziert, BORG_RSH resetten,
        # damit das System den Default-SSH-Zugriff nutzt.
        if "BORG_RSH" in env:
            del env["BORG_RSH"]

    logger.debug(f"Führe Command aus: {command}")

    process = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env
    )
    stdout, stderr = process.communicate()
    returncode = process.returncode

    if returncode == 0:
        logger.debug(f"Command erfolgreich: {command}")
    else:
        logger.error(f"Command fehlgeschlagen (Code {returncode}): {command}")
        logger.error(f"stderr: {stderr.decode('utf-8').strip()}")

    # Log stdout und stderr
    if stdout:
        logger.debug(f"stdout:\n{stdout.decode('utf-8').strip()}")
    if stderr:
        logger.debug(f"stderr:\n{stderr.decode('utf-8').strip()}")

    return returncode, stdout.decode('utf-8'), stderr.decode('utf-8')


def create_zfs_snapshots_and_mount(zfs_pools):
    """
    Erstellt für jeden Pool in zfs_pools einen Snapshot (nicht rekursiv),
    mountet diesen read-only in SCRIPT_TMP_DIR/zfs/<POOLNAME>.
    Gibt eine Liste von Dicts zurück, damit wir diese Snapshots später
    wieder aushängen und löschen können.
    """
    global backup_success

    snapshot_info = []
    if not zfs_pools:
        return snapshot_info

    logger.info("Starte Erstellung und Mounten von ZFS-Snapshots...")
    timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H-%M-%S")
    zfs_base_dir = os.path.join(SCRIPT_TMP_DIR, "zfs")

    # Unterordner "zfs" anlegen
    try:
        os.makedirs(zfs_base_dir, exist_ok=True)
    except Exception as e:
        logger.error(f"Konnte {zfs_base_dir} nicht erstellen: {e}")
        backup_success = False
        backup_fail_reasons.append(f"Konnte {zfs_base_dir} nicht erstellen")
        return snapshot_info

    for pool in zfs_pools:
        snap_name = f"{pool}@backup-snapshot_{timestamp_str}"
        mount_point = os.path.join(zfs_base_dir, pool.replace("/", "_"))  # "/" darf im Pfad nicht direkt sein
        # 1) Snapshot erstellen
        cmd_snapshot = f"zfs snapshot {snap_name}"
        rc, out, err = run_command(cmd_snapshot)
        if rc != 0:
            logger.error(f"Snapshot fehlgeschlagen für {snap_name}")
            backup_fail_reasons.append(f"Snapshot fehlgeschlagen für {snap_name}")
            backup_success = False
            continue

        # 2) Mountpoint anlegen
        try:
            os.makedirs(mount_point, exist_ok=False)
        except Exception as e:
            logger.error(f"Konnte Mountpoint {mount_point} nicht erstellen: {e}")
            backup_success = False
            backup_fail_reasons.append(f"Konnte Mountpoint {mount_point} nicht erstellen")
            # Snapshot ggf. wieder löschen
            run_command(f"zfs destroy {snap_name}")
            continue

        # 3) Snapshot read-only mounten
        cmd_mount = f"mount -t zfs -o ro {snap_name} {mount_point}"
        rc, out, err = run_command(cmd_mount)
        if rc != 0:
            logger.error(f"Mount fehlgeschlagen für {snap_name}")
            backup_success = False
            backup_fail_reasons.append(f"Mount fehlgeschlagen für {snap_name}")
            # Snapshot wieder löschen
            run_command(f"zfs destroy {snap_name}")
            # Mountpoint wieder entfernen
            try:
                os.rmdir(mount_point)
            except OSError:
                pass
            continue

        # Erfolg! Infos speichern
        snapshot_info.append({
            "pool": pool,
            "snapshot": snap_name,
            "mountpoint": mount_point
        })
        logger.info(f"Snapshot erstellt und gemountet: {snap_name} -> {mount_point}")

    return snapshot_info


def unmount_and_destroy_zfs_snapshots(snapshot_info):
    """
    Hängt alle zuvor erstellten Snapshots aus und zerstört sie.
    """
    global backup_success

    if not snapshot_info:
        return
    logger.info("Starte Unmount und Zerstörung aller ZFS-Snapshots...")

    for info in snapshot_info:
        snap_name = info["snapshot"]
        mount_point = info["mountpoint"]
        # 1) unmount
        cmd_umount = f"umount {mount_point}"
        rc, out, err = run_command(cmd_umount)
        if rc != 0:
            logger.error(f"Unmount fehlgeschlagen für {mount_point}. Manuelles Aufräumen nötig?")
            backup_fail_reasons.append(f"Unmount fehlgeschlagen für {mount_point}")
            backup_success = False
        else:
            # Mountpoint Ordner löschen
            try:
                os.rmdir(mount_point)
            except OSError as e:
                logger.warning(f"Konnte Mountpoint {mount_point} nicht entfernen: {e}")

        # 2) snapshot destroy
        cmd_destroy = f"zfs destroy {snap_name}"
        rc, out, err = run_command(cmd_destroy)
        if rc != 0:
            logger.error(f"ZFS Destroy fehlgeschlagen für {snap_name}")
            backup_fail_reasons.append(f"ZFS Destroy fehlgeschlagen für {snap_name}")
            backup_success = False


def create_backup(repo_config, directories):
    """
    Erstellt ein Borg-Backup in einem bestimmten Repository.
    directories: Liste von Pfaden, die in das Backup aufgenommen werden sollen.
    """
    global backup_success

    if not directories:
        logger.warning("Keine Verzeichnisse zum Sichern angegeben.")
        return

    repo_url = repo_config["repo_url"]
    passphrase = repo_config.get("passphrase")
    ssh_key = repo_config.get("ssh_key")

    # Backup-Name
    timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H-%M-%S")
    archive_name = f"{BACKUP_NAME_PREFIX}-{timestamp_str}"

    logger.info(f"Erstelle Backup für Repository: {repo_url}")
    logger.info(f"  -> Archivname: {archive_name}")

    # borg create
    directories_str = " ".join(directories)
    command = (
        f"borg create --stats --compression lz4 "
        f"{repo_url}::{archive_name} {directories_str}"
    )

    returncode, _, _ = run_command(command, passphrase=passphrase, ssh_key=ssh_key)
    if returncode != 0:
        backup_success = False
        backup_fail_reasons.append(f"Backup fehlgeschlagen für {repo_url}")


def verify_backups(repo_config):
    """
    Führt eine Integritätsprüfung (borg check) für das angegebene Repository durch.
    """
    global backup_success

    repo_url = repo_config["repo_url"]
    passphrase = repo_config.get("passphrase")
    ssh_key = repo_config.get("ssh_key")

    logger.info(f"Starte Repository-Check für: {repo_url}")

    # borg check [--verify-data]
    command = f"borg check {repo_url}"
    if CHECK_WITH_VERIFY_DATA:
        command += " --verify-data"

    returncode, _, _ = run_command(command, passphrase=passphrase, ssh_key=ssh_key)
    if returncode != 0:
        backup_success = False
        backup_fail_reasons.append(f"Check fehlgeschlagen für {repo_url}")


def prune_backups(repo_config):
    """
    Löscht alte Backups nach den eingestellten Parametern (daily, weekly, monthly...).
    """
    global backup_success

    repo_url = repo_config["repo_url"]
    passphrase = repo_config.get("passphrase")
    ssh_key = repo_config.get("ssh_key")

    logger.info(f"Prune Backups für Repository: {repo_url}")
    command = (
        f"borg prune -v --list {repo_url} "
        f"--keep-daily={PRUNE_KEEP_DAILY} "
        f"--keep-weekly={PRUNE_KEEP_WEEKLY} "
        f"--keep-monthly={PRUNE_KEEP_MONTHLY} "
        f"--keep-yearly={PRUNE_KEEP_YEARLY}"
    )

    returncode, _, _ = run_command(command, passphrase=passphrase, ssh_key=ssh_key)
    if returncode != 0:
        backup_success = False
        backup_fail_reasons.append(f"Prune fehlgeschlagen für {repo_url}")


def compact_repo(repo_config):
    """
    Führt borg compact durch, falls in den Einstellungen aktiviert.
    """
    global backup_success

    if not ENABLE_BORG_COMPACT:
        return

    repo_url = repo_config["repo_url"]
    passphrase = repo_config.get("passphrase")
    ssh_key = repo_config.get("ssh_key")

    logger.info(f"Borg-Compact für Repository: {repo_url}")
    command = f"borg compact {repo_url}"

    returncode, _, _ = run_command(command, passphrase=passphrase, ssh_key=ssh_key)
    if returncode != 0:
        backup_success = False
        backup_fail_reasons.append(f"Compact fehlgeschlagen für {repo_url}")


def garbage_collect_logs():
    """
    Löscht alte Logdateien, sodass nur die neuesten LOG_GARBAGE_KEEP Dateien übrig bleiben.
    Falls LOG_GARBAGE_KEEP = 0, dann wird nichts gelöscht (unendlich).
    """
    if LOG_GARBAGE_KEEP == 0:
        logger.info("Garbage Collect Logs: Unendlich Logdateien werden behalten.")
        return

    logger.info("Garbage Collect Logs: Prüfe alte Logdateien...")

    pattern = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_*.log")
    log_files = glob.glob(pattern)

    # Sortieren nach Erstellungszeit (älteste zuerst)
    log_files.sort(key=os.path.getmtime)

    total_logs = len(log_files)
    if total_logs <= LOG_GARBAGE_KEEP:
        logger.info(f"Es gibt insgesamt {total_logs} Logdateien. Keine werden gelöscht.")
        return

    to_delete_count = total_logs - LOG_GARBAGE_KEEP
    logger.info(f"Es gibt {total_logs} Logdateien. Entferne die ältesten {to_delete_count}.")

    for i in range(to_delete_count):
        file_to_delete = log_files[i]
        try:
            os.remove(file_to_delete)
            logger.info(f"Gelöscht: {file_to_delete}")
        except Exception as e:
            logger.error(f"Konnte Logdatei nicht löschen: {file_to_delete} -> {e}")

def clear_temp_directory_contents():
    """
    Removes only the contents of SCRIPT_TMP_DIR but not the directory itself.
    """
    for filename in os.listdir(SCRIPT_TMP_DIR):
        path = os.path.join(SCRIPT_TMP_DIR, filename)
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.remove(path)
            else:
                shutil.rmtree(path)
        except Exception as e:
            logger.error(f"Error removing {path}: {e}")

    logger.info("TMP directory contents cleared successfully.")


# =============================================================================
# ============================== HAUPTPROGRAMM ================================
# =============================================================================

def main():
    global backup_success

    logfile_path = setup_logging()

    # Skript-Temp-Ordner prüfen
    check_script_tmp_dir()

    # Lockfile setzen
    acquire_lock_or_exit(logfile_path)

    # Vor dem Backup prüfen, ob wir überhaupt etwas sichern können:
    dirs_empty = (len(BACKUP_DIRECTORIES) == 0)
    zfs_empty = (len(ZFS_POOLS) == 0)
    if dirs_empty and zfs_empty:
        logger.error("Weder ZFS-Pools noch Directories zum Backup konfiguriert. Abbruch.")
        release_lock()
        sys.exit(1)

    # ZFS-Snapshots erstellen und mounten (falls konfiguriert)
    zfs_snapshots = []
    try:
        if not zfs_empty:
            zfs_snapshots = create_zfs_snapshots_and_mount(ZFS_POOLS)
    except Exception as e:
        logger.exception("Fehler beim Anlegen/Mounten der ZFS-Snapshots.")
        backup_fail_reasons.append(f"Fehler beim Anlegen/Mounten der ZFS-Snapshots: {e}")
        backup_success = False

    # Directory-Liste für das Backup: reguläre Verzeichnisse + ggf. zfs-Verzeichnis
    all_backup_dirs = []
    if not dirs_empty:
        all_backup_dirs.extend(BACKUP_DIRECTORIES)
    if not zfs_empty:
        # Nur dann, wenn wir tatsächlich mind. 1 Snapshot erfolgreich erzeugt haben,
        # macht es Sinn, das ZFS-Verzeichnis ins Backup einzubeziehen.
        # (Sind alle fehlgeschlagen, ist es leer, aber wir nehmen es der Einfachheit halber trotzdem)
        zfs_base_dir = os.path.join(SCRIPT_TMP_DIR, "zfs")
        all_backup_dirs.append(zfs_base_dir)

    try:
        # 1. Backup für jedes Repository erstellen
        for repo in BORG_REPOSITORIES:
            create_backup(repo, all_backup_dirs)

        # 2. Verifizieren
        for repo in BORG_REPOSITORIES:
            verify_backups(repo)

        # 3. Prune
        for repo in BORG_REPOSITORIES:
            prune_backups(repo)

        # 4. Optionales compact
        for repo in BORG_REPOSITORIES:
            compact_repo(repo)

        # 5. Log Garbage Collect
        garbage_collect_logs()

    except Exception as e:
        backup_success = False
        backup_fail_reasons.append(f"Unerwarteter Fehler im Skriptablauf: {e}")
        logger.exception("Unerwarteter Fehler aufgetreten.")
    finally:
        # Unmount und Zerstören der ZFS-Snapshots (falls vorhanden)
        try:
            unmount_and_destroy_zfs_snapshots(zfs_snapshots)
        except Exception as e:
            logger.exception("Fehler beim Unmount/Destroy der ZFS-Snapshots.")
            backup_fail_reasons.append(f"Fehler beim Unmount/Destroy: {e}")
            backup_success = False

        # Lockfile entfernen
        release_lock()

        # Temp Directory Leeren
        clear_temp_directory_contents()

    # Zusammenfassung
    end_time = datetime.datetime.now()
    duration = end_time - start_time

    summary = []
    summary.append(f"Backup Erfolg: {backup_success}")
    summary.append(f"Start: {start_time}")
    summary.append(f"Ende:  {end_time}")
    summary.append(f"Dauer: {duration}")

    if not backup_success:
        summary.append("Fehlerursachen:")
        for reason in backup_fail_reasons:
            summary.append(f"- {reason}")

    summary_str = "\n".join(summary)

    logger.info("=== ZUSAMMENFASSUNG ===")
    logger.info(summary_str)

    # Email-Betreff
    if backup_success:
        email_subject = "[BorgBackup] Backup erfolgreich"
    else:
        short_reason = ", ".join(backup_fail_reasons)
        email_subject = f"[BorgBackup] Backup FEHLGESCHLAGEN - {short_reason}"

    # "Error only"-Modus beachten
    if not (EMAIL_ERROR_ONLY_MODE and backup_success):
        # Wenn "error only" aktiv ist UND alles ok war, keine E-Mail senden
        send_email(email_subject, summary_str, logfile_path)
    else:
        logger.info("Backup erfolgreich, 'error only' E-Mail-Modus aktiv - Keine E-Mail gesendet.")


if __name__ == "__main__":
    main()
