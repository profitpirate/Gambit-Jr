-- Emit a token only in the month containing its first observed PumpSwap trade.
WITH pumpswap AS (
    SELECT
        CASE WHEN token_bought_mint_address != 'So11111111111111111111111111111111111111112'
             THEN token_bought_mint_address ELSE token_sold_mint_address END AS token_address,
        block_time, tx_id, block_slot
    FROM dex_solana.trades
    WHERE block_time < TIMESTAMP '{{month_end}}'
      AND project_program_id = 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'
), first_trade AS (
    SELECT token_address, MIN(block_time) AS observed_at,
           MIN_BY(tx_id, block_time) AS tx_id,
           MIN_BY(block_slot, block_time) AS block_slot
    FROM pumpswap
    GROUP BY token_address
)
SELECT token_address, observed_at, 'pumpswap' AS venue, tx_id, block_slot,
       'first_pumpswap_trade' AS source
FROM first_trade
WHERE observed_at >= TIMESTAMP '{{month_start}}'
  AND observed_at < TIMESTAMP '{{month_end}}'
ORDER BY observed_at, tx_id, token_address
