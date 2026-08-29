-- Gambit Jr owned SQL. Create discriminators and table schema reviewed 2026-08-29.
WITH pump_creates AS (
    SELECT
        account_arguments[1] AS token_address,
        block_time AS observed_at,
        tx_id,
        block_slot
    FROM solana.instruction_calls
    WHERE block_time >= TIMESTAMP '{{month_start}}'
      AND block_time < TIMESTAMP '{{month_end}}'
      AND executing_account = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
      AND tx_success
      AND bytearray_substring(data, 1, 8) IN (
          0x181ec828051c0777,
          0xd6904cec5f8b31b4
      )
), tx_creators AS (
    SELECT signature AS tx_id, block_slot, signer
    FROM solana.transactions
    WHERE block_time >= TIMESTAMP '{{month_start}}'
      AND block_time < TIMESTAMP '{{month_end}}'
      AND success
)
SELECT
    creates.token_address,
    creates.observed_at,
    creators.signer AS creator,
    creates.tx_id,
    creates.block_slot,
    'solana.instruction_calls:pumpfun_create' AS source
FROM pump_creates AS creates
LEFT JOIN tx_creators AS creators
    ON creators.tx_id = creates.tx_id
   AND creators.block_slot = creates.block_slot
WHERE creates.token_address IS NOT NULL
ORDER BY creates.observed_at, creates.tx_id, creates.token_address
