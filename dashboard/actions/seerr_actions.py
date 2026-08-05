"""
Seerr-side fixes. The root-folder fix is a true read-modify-write -- deliberately NOT
reusing scripts/provision.py's configure_seerr_service(), which reconstructs the whole
settings object from its own bootstrap opinions (activeProfileId, syncEnabled, is4k,
tags, etc.). That's correct for first-time setup; here we want to touch exactly one
field and leave everything else exactly as Seerr already has it.
"""

from ..clients.seerr import SeerrClient
from ..models import ActionResult, PreviewResult


def preview_fix_root_folder(cfg, snap, params):
    service = params["service"]  # "sonarr" | "radarr"
    settings = snap.seerr_settings_sonarr if service == "sonarr" else snap.seerr_settings_radarr
    root_folders = snap.sonarr_root_folders if service == "sonarr" else snap.radarr_root_folders
    valid_paths = sorted(rf["path"] for rf in root_folders)
    current = settings.get("activeDirectory")

    if not valid_paths:
        return PreviewResult(summary=f"Can't preview -- {service}'s root folders aren't known this poll.",
                              blocked_reason=f"{service} root folder list is empty or unreachable.")
    if current in valid_paths:
        return PreviewResult(summary=f"Seerr's {service} root folder is already valid ({current}). Nothing to fix.",
                              blocked_reason="Already correct.")

    # Only one real folder to fall back to in this stack (see README section 9's
    # single-MEDIA_ROOT-mount design) -- if there's ever more than one, don't guess,
    # make the human pick.
    if len(valid_paths) != 1:
        return PreviewResult(
            summary=f"Seerr has \"{current}\" cached, which no longer exists. Multiple valid "
                    f"root folders exist ({', '.join(valid_paths)}) -- pick one by hand in Seerr's "
                    f"Settings > Services rather than via this button.",
            blocked_reason="Multiple candidate root folders -- ambiguous.",
        )
    proposed = valid_paths[0]
    return PreviewResult(
        summary=f"Seerr's cached {service} root folder is stale. Will update activeDirectory only; "
                f"every other setting (profile, tags, sync options, etc.) stays exactly as-is.",
        before={"activeDirectory": current}, after={"activeDirectory": proposed},
    )


def execute_fix_root_folder(cfg, params):
    service = params["service"]
    seerr = SeerrClient(cfg.seerr_base, cfg.seerr_key)
    existing_list = seerr.settings_for(service)
    if not existing_list:
        return ActionResult(ok=False, message=f"Seerr returned no {service} settings to fix.")
    body = dict(existing_list[0])
    service_id = body.pop("id")

    # Recompute the proposed value the same way preview() did, rather than trusting a
    # client-supplied "after" value -- params only carries the service name.
    from ..clients.arr import ArrClient
    arr_cfg = (cfg.sonarr_base, cfg.sonarr_key) if service == "sonarr" else (cfg.radarr_base, cfg.radarr_key)
    arr = ArrClient(service, *arr_cfg)
    valid_paths = sorted(rf["path"] for rf in arr.root_folders())
    if len(valid_paths) != 1:
        return ActionResult(ok=False, message="Root folder set changed since preview -- ambiguous, aborting.")

    body["activeDirectory"] = valid_paths[0]
    seerr.put_settings_for(service, service_id, body)
    return ActionResult(ok=True, message=f"Seerr's {service} activeDirectory is now {valid_paths[0]}.")


def preview_retry_request(cfg, snap, params):
    request_id = params["request_id"]
    req = next((r for r in snap.seerr_requests if r["id"] == request_id), None)
    if req is None:
        return PreviewResult(summary="Request not found in the current snapshot.", blocked_reason="not found")
    return PreviewResult(
        summary=f"Will retry request #{request_id} ({req.get('type')}).",
        before={"status": req.get("status")},
    )


def execute_retry_request(cfg, params):
    seerr = SeerrClient(cfg.seerr_base, cfg.seerr_key)
    seerr.retry_request(params["request_id"])
    return ActionResult(ok=True, message=f"Retried request #{params['request_id']}.")
