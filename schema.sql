CREATE SCHEMA IF NOT EXISTS humidificacao_csp2;

CREATE TABLE IF NOT EXISTS humidificacao_csp2.operacoes (
    record_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    inicio TIMESTAMPTZ NOT NULL,
    fim TIMESTAMPTZ,
    operation_id INTEGER NOT NULL CHECK (operation_id BETWEEN 0 AND 65535),
    programa TEXT NOT NULL,
    num_inj INTEGER CHECK (num_inj BETWEEN 0 AND 65535),
    vel_tpt INTEGER CHECK (vel_tpt BETWEEN 0 AND 65535),
    qtd_agua NUMERIC(12,2),
    hum_entrada NUMERIC,
    hum_saida NUMERIC,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT csp2_passagem_unica UNIQUE (operation_id, inicio)
);

CREATE INDEX IF NOT EXISTS csp2_passagens_inicio_idx
    ON humidificacao_csp2.operacoes (inicio);

