-- Optimistic concurrency for work orders: every successful transition bumps version.
ALTER TABLE work_orders ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1;
