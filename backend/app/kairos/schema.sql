-- Kairos chatbot schema — chatbot_* prefixed tables.
-- These run inside the single Temveris Supabase database (no separate
-- Kairos DB exists). Foreign keys reference the main `users` table.

create extension if not exists pgcrypto;

create or replace function set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = timezone('utc', now());
  return new;
end;
$$;


-- ── Conversations ─────────────────────────────────────────────────────────
create table if not exists chatbot_conversations (
  id uuid primary key default gen_random_uuid(),
  session_id text not null unique,
  user_email text references users(email) on delete set null,
  user_name text default '',
  company text default '',
  employee_id text default '',
  title text not null default 'New conversation',
  preview text default '',
  message_count integer not null default 0,
  status text not null default 'active',
  started_at timestamptz not null default timezone('utc', now()),
  last_message_at timestamptz not null default timezone('utc', now()),
  expires_at timestamptz not null,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now())
);

-- ── Messages (chat lines) ─────────────────────────────────────────────────
create table if not exists chatbot_messages (
  id uuid primary key default gen_random_uuid(),
  session_id text not null,
  user_email text references users(email) on delete set null,
  role text not null check (role in ('user', 'assistant')),
  content text not null,
  metadata jsonb not null default '{}'::jsonb,
  expires_at timestamptz not null,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now())
);

-- ── Per-user mail rate limit ──────────────────────────────────────────────
create table if not exists chatbot_email_rate_limits (
  key text primary key,
  points integer not null default 1,
  expire timestamptz not null,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now())
);

-- ── Unknown / unmatched prompts (moderation queue) ────────────────────────
create table if not exists chatbot_unknown_prompts (
  prompt text primary key,
  count integer not null default 1
);

create index if not exists idx_chatbot_conversations_user_email on chatbot_conversations (user_email);
create index if not exists idx_chatbot_conversations_expires_at on chatbot_conversations (expires_at);
create index if not exists idx_chatbot_messages_session_id on chatbot_messages (session_id);
create index if not exists idx_chatbot_messages_created_at on chatbot_messages (created_at);
create index if not exists idx_chatbot_messages_user_email on chatbot_messages (user_email);

drop trigger if exists trg_chatbot_conversations_updated_at on chatbot_conversations;
create trigger trg_chatbot_conversations_updated_at
before update on chatbot_conversations
for each row
execute function set_updated_at();

drop trigger if exists trg_chatbot_messages_updated_at on chatbot_messages;
create trigger trg_chatbot_messages_updated_at
before update on chatbot_messages
for each row
execute function set_updated_at();

drop trigger if exists trg_chatbot_email_rate_limits_updated_at on chatbot_email_rate_limits;
create trigger trg_chatbot_email_rate_limits_updated_at
before update on chatbot_email_rate_limits
for each row
execute function set_updated_at();
