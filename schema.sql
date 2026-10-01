create table if not exists calls (
  id uuid primary key default gen_random_uuid(),
  vapi_call_id text unique not null,
  phone_number text not null,
  lead_name text,
  status text not null default 'queued',       -- queued | in-progress | completed | failed
  interest_level text,                          -- hot | warm | cold | not-interested
  summary text,
  transcript text,
  recording_url text,
  duration_seconds int,
  raw_payload jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists calls_created_at_idx on calls (created_at desc);


-- Call script templates (the agent's system prompt). One row is "active" = the live script.
create table if not exists script_templates (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  prompt text not null,
  active boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table script_templates enable row level security;
