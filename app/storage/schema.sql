CREATE TABLE IF NOT EXISTS action_log (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    action_type TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_action_log_created_at ON action_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_action_log_chat_id ON action_log (chat_id);

CREATE TABLE IF NOT EXISTS private_invite_links (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    invite_link TEXT NOT NULL UNIQUE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    note TEXT
);

ALTER TABLE private_invite_links
    ADD COLUMN IF NOT EXISTS source_chat_id BIGINT;

ALTER TABLE private_invite_links
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE TABLE IF NOT EXISTS keyword_rules (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    UNIQUE (kind, value)
);

-- Legacy migration: old "intent" kind is moved into "request".
INSERT INTO keyword_rules (kind, value)
SELECT 'request', value
FROM keyword_rules
WHERE kind = 'intent'
ON CONFLICT (kind, value) DO NOTHING;

DELETE FROM keyword_rules WHERE kind = 'intent';

CREATE INDEX IF NOT EXISTS idx_keyword_rules_kind ON keyword_rules (kind);

CREATE TABLE IF NOT EXISTS runtime_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS message_audit (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    normalized_text TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_read_state (
    chat_id BIGINT PRIMARY KEY,
    last_seen_message_id BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_read_state_updated_at ON chat_read_state (updated_at DESC);

CREATE TABLE IF NOT EXISTS discovered_groups (
    id BIGSERIAL PRIMARY KEY,
    peer_id BIGINT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    username TEXT,
    source_query TEXT NOT NULL DEFAULT '',
    joined BOOLEAN NOT NULL DEFAULT FALSE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    last_error TEXT,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE discovered_groups
    ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS idx_discovered_groups_joined ON discovered_groups (joined);
CREATE INDEX IF NOT EXISTS idx_discovered_groups_active ON discovered_groups (active);
CREATE INDEX IF NOT EXISTS idx_discovered_groups_username ON discovered_groups (username);

CREATE TABLE IF NOT EXISTS bot_subscribers (
    user_id BIGINT PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    username TEXT,
    first_name TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    subscribed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bot_subscribers_active ON bot_subscribers (active);
CREATE INDEX IF NOT EXISTS idx_bot_subscribers_updated_at ON bot_subscribers (updated_at DESC);
