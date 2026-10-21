-- Accounts for the private pages (calendar, infinite buying). Run once in the Supabase SQL editor, then create the admin row with the seed script.
-- Separate from the legacy custom_users table (bcrypt, used by the old Streamlit dashboard) so that app stays untouched.
create table if not exists app_users (
  username      text primary key,
  password_hash text not null,                         -- "sha256$salt$hex" (salted SHA-256, never the plain password)
  name          text not null,
  phone         text not null,
  role          text not null default 'user'    check (role in ('user', 'admin')),
  status        text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
  created_at    timestamptz not null default now(),
  decided_at    timestamptz
);
alter table app_users enable row level security;       -- the backend uses the service key; no public policy

-- Infinite-buying cycles now belong to an account instead of carrying their own password.
alter table infinite_cycles add column if not exists owner text;
alter table infinite_cycles alter column password_hash drop not null;
update infinite_cycles set owner = 'ansdyd23' where owner is null;
create index if not exists infinite_cycles_owner_idx on infinite_cycles (owner);
