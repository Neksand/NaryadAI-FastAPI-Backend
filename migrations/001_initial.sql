CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TYPE user_role AS ENUM ('worker', 'master', 'manager', 'admin');
CREATE TYPE work_order_kind AS ENUM ('planned', 'unplanned');
CREATE TYPE work_order_priority AS ENUM ('critical', 'high', 'normal', 'planned');
CREATE TYPE work_order_status AS ENUM ('issued', 'accepted', 'queued', 'rejected', 'in_progress', 'paused', 'rework', 'done', 'ai_review', 'closed', 'cancelled');
CREATE TYPE employee_status AS ENUM ('free', 'busy', 'queue', 'off');
CREATE TYPE ai_verdict AS ENUM ('accepted', 'accepted_with_remarks', 'needs_rework', 'needs_master_review');
CREATE TYPE ai_master_decision AS ENUM ('agree_ai', 'override');
CREATE TYPE photo_kind AS ENUM ('before', 'after');
CREATE TYPE insight_status AS ENUM ('new', 'seen', 'in_plan', 'dismissed');

CREATE TABLE areas (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  name_kk text,
  code text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

CREATE TABLE crews (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  area_id uuid NOT NULL REFERENCES areas(id),
  lead_employee_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  UNIQUE (area_id, name)
);

CREATE TABLE employees (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  login citext NOT NULL UNIQUE,
  pin_hash text NOT NULL,
  full_name text NOT NULL,
  specialty text,
  grade smallint CHECK (grade IS NULL OR grade BETWEEN 1 AND 8),
  crew_id uuid REFERENCES crews(id),
  role user_role NOT NULL,
  shift text NOT NULL DEFAULT '1' CHECK (shift IN ('1', '2', 'night')),
  current_status employee_status NOT NULL DEFAULT 'off',
  lang text NOT NULL DEFAULT 'ru' CHECK (lang IN ('ru', 'kk')),
  enabled boolean NOT NULL DEFAULT true,
  failed_pin_attempts smallint NOT NULL DEFAULT 0 CHECK (failed_pin_attempts BETWEEN 0 AND 5),
  locked_at timestamptz,
  auth_version integer NOT NULL DEFAULT 0,
  last_login_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

ALTER TABLE crews ADD CONSTRAINT crews_lead_employee_fk FOREIGN KEY (lead_employee_id) REFERENCES employees(id) ON DELETE SET NULL;

CREATE TABLE employee_areas (
  employee_id uuid NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
  area_id uuid NOT NULL REFERENCES areas(id) ON DELETE CASCADE,
  PRIMARY KEY (employee_id, area_id)
);

CREATE TABLE equipment (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  name_kk text,
  inventory_no text NOT NULL UNIQUE,
  area_id uuid NOT NULL REFERENCES areas(id),
  type text NOT NULL,
  criticality text NOT NULL CHECK (criticality IN ('A', 'B', 'C')),
  qr_token text UNIQUE,
  external_id text UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

CREATE TABLE fault_codes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code text NOT NULL UNIQUE,
  fault_group text NOT NULL CHECK (fault_group IN ('М', 'Э', 'Г', 'П', 'С')),
  name text NOT NULL,
  name_kk text,
  default_norm_minutes integer CHECK (default_norm_minutes IS NULL OR default_norm_minutes > 0),
  deleted_at timestamptz
);

CREATE TABLE materials (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  name_kk text,
  unit text NOT NULL CHECK (unit IN ('шт', 'кг', 'л', 'м')),
  typical_usage_per_order numeric(12,3),
  external_id text UNIQUE,
  deleted_at timestamptz,
  UNIQUE (name)
);

CREATE TABLE time_norms (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  fault_code_id uuid NOT NULL REFERENCES fault_codes(id),
  equipment_type text NOT NULL,
  minutes integer NOT NULL CHECK (minutes > 0),
  deleted_at timestamptz,
  UNIQUE (fault_code_id, equipment_type)
);

CREATE TABLE reasons (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind text NOT NULL CHECK (kind IN ('reject', 'pause', 'cancel', 'downtime')),
  code text NOT NULL,
  name text NOT NULL,
  name_kk text,
  disrespectful boolean NOT NULL DEFAULT false,
  deleted_at timestamptz,
  UNIQUE (kind, code)
);

CREATE TABLE settings (
  key text PRIMARY KEY,
  value jsonb NOT NULL,
  updated_by uuid REFERENCES employees(id),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE work_order_counters (
  key text PRIMARY KEY,
  value bigint NOT NULL
);

INSERT INTO work_order_counters(key, value) VALUES ('number', 0);

CREATE TABLE work_orders (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  number integer NOT NULL UNIQUE,
  issued_at timestamptz NOT NULL DEFAULT now(),
  kind work_order_kind NOT NULL,
  description text NOT NULL,
  description_source text NOT NULL DEFAULT 'text' CHECK (description_source IN ('text', 'voice')),
  area_id uuid NOT NULL REFERENCES areas(id),
  equipment_id uuid NOT NULL REFERENCES equipment(id),
  assignee_id uuid REFERENCES employees(id),
  crew_id uuid REFERENCES crews(id),
  master_id uuid NOT NULL REFERENCES employees(id),
  priority work_order_priority NOT NULL DEFAULT 'normal',
  due_at timestamptz NOT NULL,
  norm_minutes integer CHECK (norm_minutes IS NULL OR norm_minutes > 0),
  status work_order_status NOT NULL DEFAULT 'issued',
  queue_position integer,
  fault_code_id uuid REFERENCES fault_codes(id),
  comment text,
  work_done_text text,
  accepted_at timestamptz,
  started_at timestamptz,
  done_at timestamptz,
  closed_at timestamptz,
  paused_minutes integer NOT NULL DEFAULT 0 CHECK (paused_minutes >= 0),
  paused_at timestamptz,
  equipment_stopped_at timestamptz,
  equipment_restored_at timestamptz,
  last_deadline_reminder_at timestamptz,
  last_overdue_alert_at timestamptz,
  last_not_accepted_alert_at timestamptz,
  escalated_at timestamptz,
  reject_reason_code text,
  return_count integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((assignee_id IS NOT NULL)::int + (crew_id IS NOT NULL)::int = 1)
);

CREATE INDEX work_orders_status_area_idx ON work_orders(status, area_id);
CREATE INDEX work_orders_assignee_status_idx ON work_orders(assignee_id, status);
CREATE INDEX work_orders_crew_status_idx ON work_orders(crew_id, status);
CREATE INDEX work_orders_equipment_issued_idx ON work_orders(equipment_id, issued_at DESC);
CREATE INDEX work_orders_issued_idx ON work_orders(issued_at DESC, id DESC);
CREATE INDEX work_orders_due_open_idx ON work_orders(due_at) WHERE status NOT IN ('done', 'ai_review', 'closed', 'rejected', 'cancelled');

CREATE TABLE work_order_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  work_order_id uuid NOT NULL REFERENCES work_orders(id),
  actor_id uuid REFERENCES employees(id) ON DELETE SET NULL,
  actor_kind text NOT NULL DEFAULT 'user' CHECK (actor_kind IN ('user', 'system', 'ai')),
  action text NOT NULL,
  at timestamptz NOT NULL DEFAULT now(),
  client_at timestamptz,
  reason_code text,
  comment text,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX work_order_events_order_time_idx ON work_order_events(work_order_id, at, id);

CREATE FUNCTION prevent_immutable_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'append-only record cannot be changed';
END;
$$;

CREATE TRIGGER work_order_events_immutable BEFORE UPDATE OR DELETE ON work_order_events FOR EACH ROW EXECUTE FUNCTION prevent_immutable_event_mutation();

CREATE TABLE photos (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  work_order_id uuid REFERENCES work_orders(id),
  kind photo_kind NOT NULL,
  object_key text NOT NULL UNIQUE,
  content_type text NOT NULL,
  size_bytes integer NOT NULL CHECK (size_bytes > 0),
  sha256 text NOT NULL,
  taken_at timestamptz NOT NULL DEFAULT now(),
  author_id uuid NOT NULL REFERENCES employees(id),
  exif jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX photos_order_kind_idx ON photos(work_order_id, kind, created_at);

CREATE TABLE material_writeoffs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  work_order_id uuid NOT NULL REFERENCES work_orders(id),
  material_id uuid NOT NULL REFERENCES materials(id),
  quantity numeric(12,3) NOT NULL CHECK (quantity > 0),
  unit text NOT NULL,
  created_by uuid NOT NULL REFERENCES employees(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX material_writeoffs_order_idx ON material_writeoffs(work_order_id);

CREATE TABLE ai_reviews (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  work_order_id uuid NOT NULL REFERENCES work_orders(id),
  attempt integer NOT NULL,
  verdict ai_verdict NOT NULL,
  score smallint CHECK (score IS NULL OR score BETWEEN 0 AND 100),
  photo_score smallint CHECK (photo_score IS NULL OR photo_score BETWEEN 1 AND 5),
  confidence numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  checks jsonb NOT NULL,
  explanation text NOT NULL,
  strengths jsonb NOT NULL DEFAULT '[]'::jsonb,
  improvements jsonb NOT NULL DEFAULT '[]'::jsonb,
  master_score smallint CHECK (master_score IS NULL OR master_score BETWEEN 0 AND 100),
  master_comment text,
  master_decision ai_master_decision,
  decided_by uuid REFERENCES employees(id),
  decided_at timestamptz,
  model text NOT NULL,
  latency_ms integer NOT NULL DEFAULT 0,
  input_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (work_order_id, attempt)
);

CREATE INDEX ai_reviews_latest_idx ON ai_reviews(work_order_id, attempt DESC);

CREATE TABLE insights (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  type text NOT NULL CHECK (type IN ('repeat_failure', 'post_ppr_failure', 'shift_correlation', 'executor_pattern', 'material_anomaly', 'failure_forecast', 'unplanned_growth')),
  severity text NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
  scope jsonb NOT NULL DEFAULT '{}'::jsonb,
  period_from date NOT NULL,
  period_to date NOT NULL,
  headline text NOT NULL,
  evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
  recommendation text NOT NULL,
  confidence numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  order_ids uuid[] NOT NULL DEFAULT '{}',
  status insight_status NOT NULL DEFAULT 'new',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX insights_status_created_idx ON insights(status, created_at DESC);

CREATE TABLE refresh_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id uuid NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
  token_hash text NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz
);

CREATE INDEX refresh_sessions_employee_idx ON refresh_sessions(employee_id, expires_at);

CREATE TABLE device_tokens (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id uuid NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
  platform text NOT NULL CHECK (platform IN ('android', 'ios', 'web')),
  token text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE idempotency_keys (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id uuid NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
  key text NOT NULL,
  request_hash text NOT NULL,
  response_status smallint,
  response_body jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL DEFAULT now() + interval '48 hours',
  UNIQUE (employee_id, key)
);

CREATE TABLE audit_log (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_id uuid REFERENCES employees(id) ON DELETE SET NULL,
  action text NOT NULL,
  entity_type text NOT NULL,
  entity_id uuid,
  request_id text,
  ip inet,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX audit_log_created_idx ON audit_log(created_at DESC);
CREATE TRIGGER audit_log_immutable BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION prevent_immutable_event_mutation();

CREATE TABLE outbox_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event text NOT NULL,
  channels text[] NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz,
  locked_until timestamptz,
  attempts smallint NOT NULL DEFAULT 0,
  last_error text
);

CREATE INDEX outbox_unpublished_idx ON outbox_events(created_at) WHERE published_at IS NULL;

CREATE TABLE report_exports (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  requested_by uuid NOT NULL REFERENCES employees(id),
  report_type text NOT NULL,
  format text NOT NULL CHECK (format IN ('pdf', 'xlsx')),
  filters jsonb NOT NULL DEFAULT '{}'::jsonb,
  scope jsonb NOT NULL DEFAULT '{}'::jsonb,
  state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'processing', 'completed', 'failed')),
  object_key text,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  expires_at timestamptz NOT NULL DEFAULT now() + interval '30 days'
);
