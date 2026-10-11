-- Jellyfin 10.11 re-points UserData at this placeholder before deleting an item (so
-- watch history survives). The row was lost in the 2026-08 full-catalog wipe; without
-- it every delete fails with "FOREIGN KEY constraint failed" and the scan of that
-- folder aborts before new items are created. Idempotent: inserts only if missing.
INSERT INTO BaseItems (Id, Type, Name, IsFolder, IsInMixedFolder, IsLocked, IsMovie, IsRepeat, IsSeries, IsVirtualItem)
SELECT '00000000-0000-0000-0000-000000000001', 'PLACEHOLDER',
       'This is a placeholder item for UserData that has been detacted from its original item',
       0, 0, 0, 0, 0, 0, 0
WHERE NOT EXISTS (SELECT 1 FROM BaseItems WHERE Id = '00000000-0000-0000-0000-000000000001');
SELECT 'placeholder rows now: ' || count(*) FROM BaseItems WHERE Id = '00000000-0000-0000-0000-000000000001';