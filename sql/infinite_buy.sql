-- Infinite-buying (Laor V2.2 / V4.0) manager, cycle-based model (user decision 2026-10-21): every cycle is its own row with its own key and password; a finished cycle is closed and the next cycle is a NEW row.
-- Run once in the Supabase SQL editor. The first block removes the earlier account-based tables of this feature (they were empty); skip it if they do not exist. The backend uses the service key; RLS is on with no public policy.

drop table if exists infinite_snapshots;
drop table if exists infinite_orders;
drop table if exists infinite_trades;
drop table if exists infinite_accounts;

create table if not exists infinite_cycles (
  id            uuid primary key default gen_random_uuid(),
  cycle_key     text not null unique,                 -- the key of this cycle, chosen by the owner (e.g. soxl-v4-2610)
  password_hash text not null,                        -- scrypt hash "scrypt$n$r$p$salt$hash" (never the plain password); the next cycle inherits it
  cycle_no      int  not null default 1,
  prev_cycle_id uuid references infinite_cycles(id),
  name          text not null,
  version       text not null check (version in ('V2.2', 'V4.0')),
  ticker        text not null,                        -- TQQQ | SOXL
  exch_code     text not null default '0537',         -- Meritz exchange vendor code: 0537 NASDAQ, 0066 AMEX, 0321 NYSE
  market_cls    text not null default 'OQ',           -- quote market: OQ NASDAQ, AX AMEX, NY NYSE
  splits        int  not null check (splits in (10, 20, 30, 40)),
  principal_usd numeric not null,                     -- capital of THIS cycle (compounding: proceeds of the previous cycle)
  compounding   boolean not null default true,
  start_date    date,
  end_date      date,
  mode          text not null default 'normal' check (mode in ('normal', 'reverse', 'quarter_stop')),
  status        text not null default 'active' check (status in ('active', 'paused', 'done')),
  t_value       numeric not null default 0,
  cash_usd      numeric not null default 0,           -- "잔금"
  qty           int     not null default 0,
  avg_price     numeric not null default 0,
  realized_usd  numeric not null default 0,           -- realised profit of this cycle (filled when it is done)
  prev_close    numeric,
  closes5       jsonb,
  extras        jsonb   not null default '{}'::jsonb, -- reverse-mode memory, t_mode, parameter overrides
  auto_order    boolean not null default false,       -- the local executor sends the order sheet only when true
  updated_at    timestamptz not null default now(),
  created_at    timestamptz not null default now()
);

create table if not exists infinite_trades (
  id          bigserial primary key,
  cycle_id    uuid not null references infinite_cycles(id) on delete cascade,
  trade_date  date not null,
  side        text not null check (side in ('buy', 'sell')),
  qty         int  not null,
  price       numeric not null,
  tag         text,                                   -- first_buy | avg_buy | star_buy | ladder | quarter_sell | target_sell | manual ...
  order_no    text,
  t_after     numeric,
  source      text not null default 'meritz' check (source in ('meritz', 'manual', 'replay')),
  raw         jsonb,
  created_at  timestamptz not null default now(),
  unique (cycle_id, order_no, trade_date, side, qty, price)
);

create table if not exists infinite_orders (
  id          bigserial primary key,
  cycle_id    uuid not null references infinite_cycles(id) on delete cascade,
  order_date  date not null,
  seq         int  not null,
  side        text not null check (side in ('buy', 'sell')),
  order_type  text not null check (order_type in ('LOC', 'LIMIT', 'MOC', 'MARKET')),
  qty         int  not null,
  price       numeric not null default 0,
  tag         text,
  status      text not null default 'planned' check (status in ('planned', 'sent', 'filled', 'partial', 'cancelled', 'failed')),
  broker_order_no text,
  result      jsonb,
  created_at  timestamptz not null default now(),
  unique (cycle_id, order_date, seq)
);

create table if not exists infinite_snapshots (
  cycle_id    uuid not null references infinite_cycles(id) on delete cascade,
  snap_date   date not null,
  t_value numeric, cash_usd numeric, qty int, avg_price numeric, star_pct numeric, close_price numeric, mode text,
  primary key (cycle_id, snap_date)
);

alter table infinite_cycles    enable row level security;
alter table infinite_trades    enable row level security;
alter table infinite_orders    enable row level security;
alter table infinite_snapshots enable row level security;
