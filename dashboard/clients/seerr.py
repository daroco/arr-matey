"""
Seerr (/api/v1) client. Endpoint shapes verified live during Phase 0: /api/v1/request
returns {pageInfo, results[], serviceErrors}; media.externalServiceId is the real
Sonarr series id / Radarr movie id (NOT tmdbId -- confirmed by cross-checking a live
request against GET /api/v3/movie/{id}, they're different numbers); Sonarr's own real
join key is tvdbId (media.tvdbId), tmdbId is Radarr-side only. /api/v1/settings/sonarr
and /settings/radarr both return a one-element list with id: 0.
"""

from .base import arr_api, SourceError


class SeerrClient:
    def __init__(self, base, key):
        self.base = base
        self.key = key

    def _get(self, path, **kwargs):
        try:
            return arr_api(self.base, self.key, "GET", path, **kwargs)
        except Exception as e:
            raise SourceError("seerr", e) from e

    def requests_page(self, take=100, skip=0):
        data = self._get("/api/v1/request", params={"take": take, "skip": skip, "sort": "added"})
        return data.get("results", []), data.get("pageInfo", {})

    def all_requests(self, max_pages=5, take=100):
        """Most-recent-first, capped at max_pages*take -- matches the same "recent
        horizon, not full history" tradeoff scripts/seedbox-cleanup.py already accepts
        for arr history, for the same reason: a single page covers the overwhelming
        majority of what anyone is actually looking at."""
        out = []
        skip = 0
        for _ in range(max_pages):
            results, page_info = self.requests_page(take=take, skip=skip)
            out.extend(results)
            skip += take
            if skip >= page_info.get("results", 0):
                break
        return out

    def settings_for(self, service):
        """service: 'sonarr' | 'radarr'. Returns the list Seerr's API actually
        returns -- callers take [0] for the default (non-4k) service, matching
        provision.py's own convention."""
        return self._get(f"/api/v1/settings/{service}") or []

    def put_settings_for(self, service, service_id, body):
        try:
            return arr_api(self.base, self.key, "PUT", f"/api/v1/settings/{service}/{service_id}", json=body)
        except Exception as e:
            raise SourceError("seerr", e) from e

    def retry_request(self, request_id):
        try:
            return arr_api(self.base, self.key, "POST", f"/api/v1/request/{request_id}/retry")
        except Exception as e:
            raise SourceError("seerr", e) from e
