-- 008: guard photo retry storms (client retries 3-4x) against duplicates.
-- One row per (author, file, kind) while pending, and per (order, author, file, kind)
-- once linked. INSERT uses ON CONFLICT DO NOTHING + re-read; loser cleans its S3 key.
CREATE UNIQUE INDEX IF NOT EXISTS photos_pending_dedup_uidx
  ON photos(author_id, sha256, kind) WHERE work_order_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS photos_order_dedup_uidx
  ON photos(work_order_id, author_id, sha256, kind) WHERE work_order_id IS NOT NULL;
