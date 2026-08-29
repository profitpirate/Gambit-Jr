WITH pump_transactions AS (
    SELECT block_time, block_slot, signature, signer, post_token_balances
    FROM solana.transactions
    WHERE block_time >= TIMESTAMP '{{month_start}}'
      AND block_time < TIMESTAMP '{{month_end}}'
      AND success
      AND contains(account_keys, '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P')
), launches AS (
    SELECT
        balance.mint AS token_address,
        block_time AS observed_at,
        signer AS creator,
        signature AS tx_id,
        block_slot,
        row_number() OVER (PARTITION BY balance.mint ORDER BY block_time, signature) AS occurrence
    FROM pump_transactions
    CROSS JOIN UNNEST(post_token_balances) AS u(balance)
    WHERE balance.mint IS NOT NULL
)
SELECT token_address, observed_at, creator, tx_id, block_slot, 'pumpfun_monthly_universe' AS source
FROM launches
WHERE occurrence = 1
ORDER BY observed_at, tx_id, token_address
