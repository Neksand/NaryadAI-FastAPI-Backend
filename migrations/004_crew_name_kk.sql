-- Двуязычные справочники: казахское название бригады (у остальных таблиц name_kk уже есть).
ALTER TABLE crews ADD COLUMN IF NOT EXISTS name_kk text;
