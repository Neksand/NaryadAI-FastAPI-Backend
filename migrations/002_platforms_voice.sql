-- 002: desktop/tauri push platforms + voice note transcripts
ALTER TABLE device_tokens DROP CONSTRAINT IF EXISTS device_tokens_platform_check;
ALTER TABLE device_tokens ADD CONSTRAINT device_tokens_platform_check
  CHECK (platform IN ('android', 'ios', 'web', 'desktop', 'tauri'));

ALTER TABLE photos ADD COLUMN IF NOT EXISTS exif_taken_at timestamptz;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS phash text;

CREATE TABLE IF NOT EXISTS voice_notes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  work_order_id uuid REFERENCES work_orders(id),
  object_key text NOT NULL UNIQUE,
  transcript text,
  provider text NOT NULL DEFAULT 'local',
  author_id uuid NOT NULL REFERENCES employees(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
