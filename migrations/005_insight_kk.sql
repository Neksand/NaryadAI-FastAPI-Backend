-- Двуязычные инсайты: казахские заголовок и рекомендация (ИИ/правила генерят оба языка).
ALTER TABLE insights ADD COLUMN IF NOT EXISTS headline_kk text;
ALTER TABLE insights ADD COLUMN IF NOT EXISTS recommendation_kk text;
