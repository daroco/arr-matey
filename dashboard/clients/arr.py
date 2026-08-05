"""
Sonarr/Radarr client -- both speak identical /api/v3, same as provision.py treats them
(a single set of calls parameterized by base/key). Endpoint shapes below were verified
live against this stack's actual Sonarr/Radarr during Phase 0 of the approved plan, not
assumed from generic *arr docs -- see the plan file's "Assumptions" section for the full
list of what was checked.
"""

from .base import arr_api, SourceError


class ArrClient:
    def __init__(self, name, base, key):
        self.name = name   # "sonarr" | "radarr" -- only used for error labeling
        self.base = base
        self.key = key

    def _get(self, path, **kwargs):
        try:
            return arr_api(self.base, self.key, "GET", path, **kwargs)
        except Exception as e:
            raise SourceError(self.name, e) from e

    def _put(self, path, json_body):
        try:
            return arr_api(self.base, self.key, "PUT", path, json=json_body)
        except Exception as e:
            raise SourceError(self.name, e) from e

    def queue(self, page_size=250):
        # includeUnknownSeriesItems needed for Radarr's shape too even though the
        # param name says "series" -- confirmed against a live Sonarr instance;
        # harmless no-op if Radarr ignores it.
        data = self._get(
            "/api/v3/queue",
            params={"pageSize": page_size, "includeUnknownSeriesItems": "true"},
        )
        return data.get("records", [])

    def history_page(self, page_size=250):
        data = self._get(
            "/api/v3/history",
            params={"pageSize": page_size, "sortKey": "date", "sortDirection": "descending"},
        )
        return data.get("records", [])

    def history_for_series(self, series_id):
        """Sonarr only. Returns a plain list (not the paginated {records:[]} wrapper
        /api/v3/history uses) -- confirmed live, this is a real shape difference."""
        return self._get("/api/v3/history/series", params={"seriesId": series_id}) or []

    def history_for_movie(self, movie_id):
        """Radarr only. Same plain-list shape as history_for_series."""
        return self._get("/api/v3/history/movie", params={"movieId": movie_id}) or []

    def all_series(self):
        return self._get("/api/v3/series") or []

    def all_movies(self):
        return self._get("/api/v3/movie") or []

    def episodes_for_series(self, series_id, season_number=None):
        params = {"seriesId": series_id}
        if season_number is not None:
            params["seasonNumber"] = season_number
        return self._get("/api/v3/episode", params=params) or []

    def root_folders(self):
        return self._get("/api/v3/rootfolder") or []

    def indexers(self):
        return self._get("/api/v3/indexer") or []

    def indexer(self, indexer_id):
        return self._get(f"/api/v3/indexer/{indexer_id}")

    def update_indexer(self, indexer_id, body):
        return self._put(f"/api/v3/indexer/{indexer_id}", body)
