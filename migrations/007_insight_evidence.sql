-- Evidence links for insights: sample size and honest method description.
ALTER TABLE insights ADD COLUMN IF NOT EXISTS denominator integer;
ALTER TABLE insights ADD COLUMN IF NOT EXISTS formula text;
