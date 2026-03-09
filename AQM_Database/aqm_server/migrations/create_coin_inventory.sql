CREATE TABLE coin_inventory(
    record_id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL,
    key_id VARCHAR(36) NOT NULL,

    coin_category VARCHAR(6) NOT NULL CHECK ( coin_category IN ('GOLD' , 'SILVER' , 'BRONZE') ),
    public_key_blob BYTEA NOT NULL,
    signature_blob BYTEA NOT NULL,

    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fetched_by UUID DEFAULT NULL,
    fetched_at TIMESTAMPTZ DEFAULT NULL,

    CONSTRAINT uq_user_key UNIQUE (user_id , key_id)
);

CREATE INDEX idx_coin_lookup ON coin_inventory (user_id , coin_category , uploaded_at ASC) WHERE fetched_by IS NULL;
CREATE INDEX idx_coin_expiry ON coin_inventory (uploaded_at) WHERE fetched_by IS NULL;
CREATE INDEX idx_coin_hard_delete ON coin_inventory (fetched_at) WHERE fetched_by IS NOT NULL;
