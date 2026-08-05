"""Force a Prowlarr -> Sonarr/Radarr indexer resync -- lifts scripts/provision.py's
exact command-poll loop via clients/prowlarr.py."""

from ..clients.prowlarr import ProwlarrClient
from ..models import ActionResult, PreviewResult


def preview_sync(cfg, snap, params):
    return PreviewResult(
        summary="Will trigger Prowlarr's ApplicationIndexerSync and wait up to 30s for it to "
                "complete. This pushes Prowlarr's current indexer list to Sonarr/Radarr -- "
                "note this can also overwrite per-indexer fields (like Minimum Seeders) back "
                "to Prowlarr's own values if they've drifted.",
    )


def execute_sync(cfg, params):
    prowlarr = ProwlarrClient(cfg.prowlarr_base, cfg.prowlarr_key)
    ok, status = prowlarr.trigger_app_indexer_sync()
    if ok:
        return ActionResult(ok=True, message="Indexer sync completed.")
    return ActionResult(ok=False, message=f"Indexer sync did not complete: {status.get('status')}", detail=str(status))
