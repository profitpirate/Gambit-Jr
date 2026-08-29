-- One minute-level token price observation after aggregating Dune route segments.
-- Minute resolution preserves the path needed for outcome reconstruction without
-- materializing and globally sorting tens of millions of transaction-level rows.
WITH raw AS (
    SELECT
        CASE WHEN token_bought_mint_address != 'So11111111111111111111111111111111111111112'
             THEN token_bought_mint_address ELSE token_sold_mint_address END AS token_address,
        date_trunc('minute', block_time) AS observed_at,
        CASE WHEN token_bought_mint_address != 'So11111111111111111111111111111111111111112'
             THEN token_bought_amount ELSE token_sold_amount END AS token_amount,
        amount_usd, tx_id, block_slot
    FROM dex_solana.trades
    WHERE block_time >= TIMESTAMP '{{month_start}}'
      AND block_time < TIMESTAMP '{{month_end}}'
      AND block_month = CAST(date_trunc('month', TIMESTAMP '{{month_start}}') AS DATE)
      AND project IN ('pumpdotfun', 'pumpswap')
), aggregated AS (
    SELECT token_address, observed_at, SUM(token_amount) AS token_amount,
           SUM(amount_usd) AS amount_usd, MIN(tx_id) AS tx_id,
           MIN(block_slot) AS block_slot
    FROM raw
    GROUP BY token_address, observed_at
)
SELECT token_address, observed_at,
       CASE WHEN token_amount > 0 THEN amount_usd / token_amount ELSE NULL END AS price_usd,
       amount_usd, tx_id, block_slot, 'dex_solana.trades:outcome_path' AS source
FROM aggregated
