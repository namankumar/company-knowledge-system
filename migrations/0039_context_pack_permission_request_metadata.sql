-- 0039_context_pack_permission_request_metadata: durable request envelope for context packs.

ALTER TABLE context_packs
    ADD COLUMN IF NOT EXISTS policy_version TEXT NOT NULL DEFAULT 'permissioned-company-brain-v1',
    ADD COLUMN IF NOT EXISTS actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS requester_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS target_audience TEXT NOT NULL DEFAULT 'restricted',
    ADD COLUMN IF NOT EXISTS destination TEXT;

CREATE INDEX IF NOT EXISTS idx_context_packs_permission_request
    ON context_packs (tenant_id, target_audience, destination, policy_version, created_at DESC);
