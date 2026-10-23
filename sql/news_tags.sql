-- news tags (2026-10-23): keywords of every article and of every stock; an article reaches a stock when the tag lists overlap.
-- Run once in the Supabase SQL editor. The backend and the news job use the service key; RLS stays on with no public policy.
alter table market_news add column if not exists tags text[] not null default '{}';
create index if not exists market_news_tags_gin on market_news using gin (tags);

create table if not exists stock_tags (
  symbol     text primary key,
  tags       text[] not null default '{}',
  updated_at timestamptz not null default now()
);
create index if not exists stock_tags_tags_gin on stock_tags using gin (tags);
alter table stock_tags enable row level security;

-- source link of every article (2026-10-23): sent by the scheduled collector (POST /api/news/ingest); duplicates are also caught by the same URL.
alter table market_news add column if not exists source_url text;

-- when the server stored the article (2026-10-23); created_at stays the publication time. Lets a reader tell "published" from "known to us since".
alter table market_news add column if not exists ingested_at timestamptz;
