"""
Pulls finished downloads from the seedbox down to local staging folders, one client at
a time. Run via pythonw.exe as the scheduled task action -- pythonw has no console, so
no hidden-window wrapper is needed (unlike a plain console app such as rclone.exe or
powershell.exe launched directly from Task Scheduler).

--min-age 30s skips files still being written remotely; rclone also writes to a
.partial temp name and renames atomically on completion, which independently guards
against Sonarr/Radarr importing a half-copied file.
"""

import subprocess

RCLONE = (r"C:\Users\drcor\AppData\Local\Microsoft\WinGet\Packages\Rclone.Rclone_"
          r"Microsoft.Winget.Source_8wekyb3d8bbwe\rclone-v1.74.3-windows-amd64\rclone.exe")

SYNCS = [
    ("seedbox:/home/seedit4me/torrents/qbittorrent/ratio", r"D:\media\downloads\seedbox",
     r"D:\appdata\rclone-sync.log"),
    ("seedbox:/home/seedit4me/torrents/transmission/downloads", r"D:\media\downloads\seedbox-transmission",
     r"D:\appdata\rclone-sync-transmission.log"),
]


def main():
    for remote, local, log_file in SYNCS:
        subprocess.run(
            [RCLONE, "sync", remote, local, "--min-age", "30s",
             "--log-file", log_file, "--log-level", "INFO"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


if __name__ == "__main__":
    main()
